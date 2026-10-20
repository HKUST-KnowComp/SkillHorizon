"""Shared execution rules, independently reapplied by the scorer to raw evidence."""

from dataclasses import dataclass
from copy import deepcopy

from jsonschema import Draft202012Validator
from .argument_feedback import schema_rejection


RUN_VERSION = 6
RESOURCE_STOPS = {"context_limit", "output_limit", "incomplete_output"}


def error_output(code, message):
    return {"ok": False, "error": {"code": code, "message": message}}


def _typed_numbers(value, schema):
    if schema.get("type") == "integer" and type(value) is float and value.is_integer():
        return int(value)
    if isinstance(value, dict):
        return {key: _typed_numbers(item, schema.get("properties", {}).get(key, {})) for key, item in value.items()}
    if isinstance(value, list):
        return [_typed_numbers(item, schema.get("items", {})) for item in value]
    return deepcopy(value)


def decide_calls(calls, seen, tools, turn, *, allow_multiple=False, remaining_calls=None):
    schemas = {tool["name"]: tool["parameters"] for tool in tools}
    reserved = seen | {call["call_id"] for call in calls}
    decisions = []
    for index, call in enumerate(calls, 1):
        duplicate = call["call_id"] in seen
        transcript_id = call["call_id"]
        if duplicate:
            transcript_id = f"rejected-{turn}-{index}"
            while transcript_id in reserved:
                transcript_id += "-rejected"
        seen.update((call["call_id"], transcript_id))
        reserved.add(transcript_id)
        rejection = None
        if len(calls) > 1 and not allow_multiple:
            rejection = error_output("MULTIPLE_CALLS", "Make one skill call per turn.")
        elif duplicate:
            rejection = error_output("DUPLICATE_CALL_ID", "Use a new function call ID.")
        elif call["arguments"] is None:
            rejection = error_output("INVALID_ARGUMENTS", "Skill arguments must be a JSON object.")
        elif call["name"] not in schemas:
            rejection = error_output("UNKNOWN_SKILL", "This skill is not in the available library.")
        else:
            rejection = schema_rejection(Draft202012Validator(schemas[call["name"]]), call["arguments"])
        if rejection is None and remaining_calls is not None:
            if remaining_calls == 0:
                rejection = error_output("TOOL_BUDGET", "The tool execution budget is exhausted.")
            else:
                remaining_calls -= 1
        decisions.append({"call_id": call["call_id"], "transcript_call_id": transcript_id,
                          "rejection": rejection,
                          "arguments": _typed_numbers(call["arguments"], schemas.get(call["name"], {}))})
    return decisions


@dataclass
class ExecutionPolicy:
    config: object
    maximum_turns: int
    maximum_requests: int
    usable_turns: int = 0
    tool_calls: int = 0
    infrastructure_failures: int = 0
    resource_truncations: int = 0
    environment_failures: int = 0
    accepted_tokens: int = 0
    rejected_tokens: int = 0
    resource_tokens: int = 0
    last_counted_request: int = 0
    used_tokens: int = 0
    no_call_turns: int = 0
    identical_failures: int = 0
    previous_failure: object = None
    compaction_turns: int = 0
    compaction_tokens: int = 0
    compaction_failures: int = 0
    unchanged_request_retries: int = 0
    configuration_retry_pending: bool = False
    prompt_rejection_retry_pending: bool = False
    rate_limit_retry_pending: bool = False
    transport_retry_pending: bool = False

    @classmethod
    def for_task(cls, config, task):
        calls = task.get("reference_tool_call_count", task["remaining_skill_count"])
        turns = config.max_turns or 3 * calls
        retries = max(config.provider_contract_retries, config.prompt_rejection_retries,
                      config.rate_limit_retries, config.transport_retries)
        # With no lifetime infra cap, the shared per-input retry limit still
        # bounds total attempts. Explicit historical caps retain their meaning.
        slots = turns + (config.max_compactions if config.context_policy == "summary" else 0)
        automatic = (slots + config.max_infrastructure_failures
                     if config.max_infrastructure_failures is not None else slots * (1 + retries))
        requests = config.max_provider_requests or automatic
        return cls(config, turns, requests)

    def advance(self, turn, status, calls, results, usage, succeeded, *, retryable_configuration=False,
                retryable_rejection=False, retryable_rate_limit=False, retryable_transport=False,
                response_accepted=None, role="actor"):
        self.configuration_retry_pending = False
        self.prompt_rejection_retry_pending = False
        self.rate_limit_retry_pending = False
        self.transport_retry_pending = False
        accepted = status == "completed" if response_accepted is None else response_accepted
        if turn > self.last_counted_request:
            self.last_counted_request = turn
            if usage is not None:
                tokens = usage["total_tokens"]
                self.used_tokens += tokens
                if accepted:
                    self.accepted_tokens += tokens
                elif status in RESOURCE_STOPS:
                    self.resource_tokens += tokens
                else:
                    self.rejected_tokens += tokens
            if role == "compaction" and usage is not None:
                self.compaction_tokens += usage["total_tokens"]
            if accepted and role == "actor":
                self.usable_turns += 1
                self.tool_calls += sum(result["executed"] for result in results)
            elif accepted:
                self.compaction_turns += 1
            if status == "compaction_error":
                self.compaction_failures += 1
            elif status == "local_error":
                self.environment_failures += 1
            elif status in RESOURCE_STOPS:
                self.resource_truncations += 1
            elif not accepted:
                self.infrastructure_failures += 1
        if status != "completed":
            configuration = status in {"provider_contract_error", "provider_measurement_error"} and retryable_configuration
            rejection = status == "provider_error" and retryable_rejection
            rate_limit = status == "provider_error" and retryable_rate_limit
            transport = status == "provider_error" and retryable_transport
            limit = (self.config.provider_contract_retries if configuration else self.config.prompt_rejection_retries
                     if rejection else self.config.rate_limit_retries if rate_limit else
                     self.config.transport_retries if transport else 0)
            if self.unchanged_request_retries < limit:
                if self.config.max_total_tokens is not None and self.used_tokens >= self.config.max_total_tokens:
                    return "cost_budget", False
                if self.usable_turns >= self.maximum_turns:
                    return "turn_budget", False
                if turn >= self.maximum_requests:
                    return "request_budget", False
                if (self.config.max_infrastructure_failures is not None
                        and self.infrastructure_failures >= self.config.max_infrastructure_failures):
                    return "infrastructure_budget", False
                self.unchanged_request_retries += 1
                self.configuration_retry_pending = configuration
                self.prompt_rejection_retry_pending = rejection
                self.rate_limit_retry_pending = rate_limit
                self.transport_retry_pending = transport
                return None, False
            return status, False
        self.unchanged_request_retries = 0
        if role == "compaction":
            if self.config.max_total_tokens is not None and self.used_tokens >= self.config.max_total_tokens:
                return "cost_budget", False
            if turn >= self.maximum_requests:
                return "request_budget", False
            return None, False
        self.no_call_turns = self.no_call_turns + 1 if not calls else 0
        progressed = any(result["committed_node"] is not None for result in results)
        failure = [(call["name"], call["arguments"]) for call in calls]
        if calls and not progressed:
            self.identical_failures = self.identical_failures + 1 if failure == self.previous_failure else 1
            self.previous_failure = failure
        else:
            self.identical_failures, self.previous_failure = 0, None
        stop = None
        if succeeded:
            stop = "success"
        elif self.config.max_tool_calls is not None and self.tool_calls >= self.config.max_tool_calls:
            stop = "tool_budget"
        elif self.no_call_turns >= self.config.max_no_call_turns:
            stop = "no_skill_call"
        elif self.identical_failures >= self.config.max_identical_failures:
            stop = "stagnation"
        elif self.config.max_total_tokens is not None and self.used_tokens >= self.config.max_total_tokens:
            stop = "cost_budget"
        elif self.usable_turns >= self.maximum_turns:
            stop = "turn_budget"
        elif turn >= self.maximum_requests:
            stop = "request_budget"
        return stop, not calls and stop is None


def continuation_feedback(task):
    return f"Continue active project {task['task_id']} with the available tools."
