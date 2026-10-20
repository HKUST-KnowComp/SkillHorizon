import asyncio
from copy import deepcopy
from pathlib import Path
import shutil
import time
from uuid import uuid4

from .mcp_tools import provider_tools_from_mcp_listing
from ..util import append_jsonl, load_json, load_jsonl, write_json
from .config import RunConfig
from .messages import accountable_usage, append_turn, parse_response, request_payload
from .provider_contract import check_provider_response, response_status, retryable_configuration_response, budget_usage
from .project_runtime import project_runtime, project_server
from .scoring import score_run
from .tasks import load_task
from .transport import (FixtureTransport, HttpTransport, ProviderFailure,
                        retryable_prompt_rejection, retryable_transport_failure)
from .execution import RUN_VERSION, ExecutionPolicy, continuation_feedback, decide_calls, error_output
from .context_metrics import request_observation, binding_positions
from .rate_limits import is_rate_limit, retry_schedule, wait_for_retry
from .context import prepare_request, apply_summary, summary_config, public_task


def run_task(
    task_root: Path | str,
    output: Path | str,
    config: RunConfig,
    transport: FixtureTransport | HttpTransport,
) -> dict:
    if not isinstance(transport, (FixtureTransport, HttpTransport)):
        raise TypeError("Use FixtureTransport or HttpTransport")
    if isinstance(transport, HttpTransport) and (
        transport.description["retries"] != config.retries
        or transport.description["timeout_seconds"] != config.timeout_seconds
    ):
        raise ValueError("Transport retry/timeout differs from run configuration")
    task_root, output = Path(task_root).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(task_root):
        raise ValueError("Run output must be fresh and outside the source task")
    task = load_task(task_root)
    from .internal_mcp import FORMAT as INTERNAL_FORMAT
    if task["format"] == INTERNAL_FORMAT:
        raise ValueError("Internal observational MCP tasks use the native SDK framework: native-prepare/native-run, not the legacy project-specific loop")
    output.mkdir(parents=True)
    shutil.copytree(task_root / "checkpoint", output / "checkpoint")
    shutil.copytree(task_root / "checkpoint", output / "runtime")
    write_json(output / "task.json", task)
    write_json(output / "public_task.json", public_task(task))
    run = {
        "version": RUN_VERSION,
        "run_id": str(uuid4()),
        "task_root": str(task_root),
        "config": config.to_dict(),
        "transport": transport.description,
        "stop_reason": "interrupted",
    }
    write_json(output / "run.json", run)
    return asyncio.run(_run(task_root, output, task, config, transport, run))


async def _run(
    task_root: Path,
    root: Path,
    task: dict,
    config: RunConfig,
    transport: FixtureTransport | HttpTransport,
    run: dict,
    restored=None,
) -> dict:
    from mcp import Client

    messages = deepcopy(task["messages"])
    runtime = root / "runtime"
    policy = ExecutionPolicy.for_task(config, task)
    seen = {m["call_id"] for m in messages if m.get("type") == "function_call"}
    first_turn, pending_wait = 1, None
    if restored is not None:
        messages, policy, seen, first_turn, pending_wait = restored
    stop = "request_budget"
    server = project_server(task, runtime)
    async with Client(server) as client:
        tools = provider_tools_from_mcp_listing(await client.list_tools())
        if tools != task["tools"]:
            raise ValueError("Current MCP tools differ from task tools")
        for turn in range(first_turn, policy.maximum_requests + 1):
            if pending_wait is not None:
                await wait_for_retry(*pending_wait, isinstance(transport, HttpTransport))
                pending_wait = None
            directory = root / "turns" / f"{turn:04d}"
            role, payload, compact_plan, capacity_stop = prepare_request(
                config, messages, tools, task, policy.compaction_turns)
            if capacity_stop:
                stop = capacity_stop
                run["context_stop"] = {"before_turn": turn, "reason": stop}
                break
            request_config = summary_config(config) if role == "compaction" else config
            current = project_runtime(task, runtime)
            observation = request_observation(payload, current.read_project(), current.score())
            write_json(directory / "request.json", payload)
            expected_payload = deepcopy(payload)
            event = {
                "turn": turn,
                "status": "completed",
                "results": [],
                "feedback": False,
                "response_accepted": False,
                "transport": transport.description,
                "request_observation": observation,
                "role": role,
            }
            calls, usage, raw = [], None, None
            retryable_configuration = False
            rate_schedule = None
            try:
                raw, metadata = transport.send(payload)
                if payload != expected_payload:
                    raise ValueError("Transport modified the serialized request")
                event["transport"] = metadata
                write_json(directory / "response.json", raw)
                calls, usage = parse_response(raw, config.model)
                event["input_bindings"] = binding_positions(observation, calls)
            except ProviderFailure as exc:
                event.update(status="provider_error", error=exc.to_dict())
                event["transport"] = {**transport.description,
                    **{key: exc.details[key] for key in ("http_status", "transport_attempt_count") if key in exc.details}}
                write_json(directory / "error.json", exc.to_dict())
                stop = "provider_error"
                if is_rate_limit(event["error"]) and config.rate_limit_retries:
                    event["rate_limit_observed_at"] = time.time()
                    number = policy.unchanged_request_retries + 1
                    if number <= config.rate_limit_retries:
                        rate_schedule = retry_schedule(event["error"], number, event["rate_limit_observed_at"])
            except (ValueError, TypeError) as exc:
                usage = accountable_usage(raw)
                event.update(status="invalid_response", error=str(exc))
                stop = "invalid_response"
            else:
                event["provider_check"] = check_provider_response(expected_payload, raw, config.provider_policy,
                                                                  config.provider_instruction_template)
                status = response_status(raw, request_config, event["provider_check"])
                retryable_configuration = retryable_configuration_response(raw, request_config, event["provider_check"])
                if status != "completed":
                    stop = event["status"] = status
                else:
                    event["response_accepted"] = True
                    if role == "compaction":
                        try:
                            updated, record = apply_summary(config, messages, tools, raw, compact_plan)
                            messages = updated
                            event["compaction"] = record
                        except ValueError as exc:
                            event.update(status="compaction_error", summary_error=str(exc), response_accepted=False)
                    decisions = [] if role == "compaction" else decide_calls(calls, seen, tools, turn,
                        allow_multiple=config.parallel_tool_calls,
                        remaining_calls=None if config.max_tool_calls is None else config.max_tool_calls - policy.tool_calls)
                    for call, decision in zip(calls, decisions):
                        result = decision["rejection"]
                        executed = False
                        committed = None
                        if result is None:
                            before = load_jsonl(runtime / "model_skill_actions.jsonl")
                            try:
                                response = await client.call_tool(call["name"], decision["arguments"])
                                executed = True
                                result = response.structured_content
                            except Exception as exc:
                                result = error_output("LOCAL_TOOL_ERROR", "The MCP invocation failed before a structured result was returned.")
                                event["local_error"] = {"type": type(exc).__name__}
                                stop = event["status"] = "local_error"
                            if not isinstance(result, dict):
                                result = error_output(
                                    "LOCAL_TOOL_ERROR",
                                    "The environment returned no structured result.",
                                )
                                stop = event["status"] = "local_error"
                            after = load_jsonl(runtime / "model_skill_actions.jsonl")
                            if len(after) == len(before) + 1:
                                committed = after[-1]["node_id"]
                            elif len(after) != len(before):
                                stop = event["status"] = "local_error"
                        event["results"].append(
                            {
                                "call_id": call["call_id"],
                                "transcript_call_id": decision["transcript_call_id"],
                                "executed": executed,
                                "committed_node": committed,
                                "output": result,
                            }
                        )
                        if event["status"] == "local_error":
                            break
            succeeded = project_runtime(task, runtime).score()["termination_ready"]
            terminal, event["feedback"] = policy.advance(turn, event["status"], calls,
                event["results"], budget_usage(usage, event.get("provider_check"), config), succeeded,
                retryable_configuration=retryable_configuration,
                retryable_rejection=retryable_prompt_rejection(event.get("error", {}))
                    if event["status"] == "provider_error" else False,
                retryable_transport=retryable_transport_failure(event.get("error", {}))
                    if event["status"] == "provider_error" else False,
                retryable_rate_limit=rate_schedule is not None, response_accepted=event["response_accepted"], role=role)
            if policy.configuration_retry_pending:
                event["configuration_retry"] = True
            if policy.prompt_rejection_retry_pending:
                event["prompt_rejection_retry"] = True
            if policy.transport_retry_pending:
                event["transport_retry"] = True
            if policy.rate_limit_retry_pending:
                event["rate_limit_retry"] = rate_schedule
                pending_wait = (directory / "retry_wait.json", rate_schedule)
            if event["feedback"]:
                event["feedback_text"] = continuation_feedback(task)
            if event["status"] == "completed" and role == "actor":
                append_turn(messages, raw, event["results"], event["feedback"], event.get("feedback_text"))
            write_json(root / "active_context.json", messages)
            append_jsonl(root / "events.jsonl", event)
            if terminal:
                stop = terminal
                break
    run["stop_reason"] = stop
    write_json(root / "run.json", run)
    return score_run(root)
