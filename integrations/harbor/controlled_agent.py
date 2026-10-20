"""Text-JSON tool agent: no terminal, agent filesystem, or hidden-score feedback."""

import json
import math
from pathlib import Path
import time
import traceback

from jsonschema import Draft202012Validator

from .backend import write_record, append_event
from .execution_clock import ExecutionClock, MODE as EXECUTION_TIME_MODE, LEGACY
from .client import Client, HTTPError, DECLARATION_PROTOCOL
from .declarations import strict_json
from .private_gateway import PrivateGateway
from .provider_guard import GuardError, _encode
from . import action_protocol_v3, action_protocol_v4, action_protocol_v5, action_protocol_v6, action_protocol_v7, action_protocol_v8
from .output_recovery import OutputLimitFeedback
from .zero_b_recovery import ZeroBRecovery, VERSION as ZERO_B_VERSION
from .response_feedback import ResponseIssue, response_issue, validate_actor_response, rejection_feedback
from .response_diagnostics import (VERSION as RESPONSE_DIAGNOSTICS_VERSION,
    LEGACY_COUNT_DEFINITION, ResponseDiagnostics)


ALLOWED = frozenset({"read_project", "read_table", "read_skill_file", "search_objects",
                     "read_object", "execute_step", "run_skill_script", "begin_skill", "finish_skill"})
FIELDOPS_ALLOWED = (ALLOWED - {"read_table"}) | {"read_artifact"}
DISPATCH_ALLOWED = ALLOWED | FIELDOPS_ALLOWED
COMPACT_AT = 200000
WALL_TIME_SECONDS = None
STRICT_ACTION_PROTOCOL = "strict_json_v1"
PREFIX_ACTION_PROTOCOL = "leading_prose_actions_v2"
CURRENT_ACTION_PROTOCOL = action_protocol_v3.PROTOCOL
TEXT_ACTION_PROTOCOLS = frozenset({CURRENT_ACTION_PROTOCOL, action_protocol_v4.PROTOCOL, action_protocol_v5.PROTOCOL, action_protocol_v6.PROTOCOL, action_protocol_v7.PROTOCOL, action_protocol_v8.PROTOCOL})
ACTION_PROTOCOLS = frozenset({STRICT_ACTION_PROTOCOL, PREFIX_ACTION_PROTOCOL}) | TEXT_ACTION_PROTOCOLS
HARNESSES = {STRICT_ACTION_PROTOCOL: "controlled_text_json_v1",
            PREFIX_ACTION_PROTOCOL: "controlled_text_json_v2",
            CURRENT_ACTION_PROTOCOL: "controlled_text_json_v3",
            action_protocol_v4.PROTOCOL: "controlled_text_json_v4",
            action_protocol_v5.PROTOCOL: "controlled_text_json_v5",
            action_protocol_v6.PROTOCOL: "controlled_text_json_v6",
            action_protocol_v7.PROTOCOL: "controlled_text_json_v7",
            action_protocol_v8.PROTOCOL: "controlled_text_json_v8"}
MAX_ACTION_PREFIX_CHARS = 1024
SYSTEM = """Complete the user's business project through the public tools below.
Return exactly one JSON object, without markdown. To call tools return
{"type":"actions","actions":[{"name":"TOOL","arguments":{},"skill_run_id":"OPTIONAL_LABEL"}]}.
You may issue 1 to 32 independent calls per response. All arguments must be explicit
and fixed before receiving results. To use a returned value, wait for the next turn.
No code, expressions, references to future results, or controller-generated arguments
are executed.
Use {"type":"final","text":"your final answer"} to request termination
of the entire task, whether complete or incomplete.
Accurately state what was completed and what remains unfinished.
If incomplete, explain why you are ending based on the public
information available to you.
Use actions to continue working; final is not a progress update.
Read skill instructions using read_skill_file. Fixed scripts are available only via
run_skill_script. There is no terminal, arbitrary code execution or personal workspace.
begin_skill declares a reusable skill; finish_skill completed is your claim of local
completion, not business certification. skill_run_id is optional envelope metadata,
not a business argument. Business state and artifacts persist through public tools.
No hidden evaluator feedback is available.
"""
SUMMARY = """Summarize the model-visible business conversation for continuation.
Preserve the user's goal, skill decisions, actual artifact IDs and dependencies,
source revisions, notices, errors, unresolved obligations and next intended steps.
Distinguish verified tool results from your guesses. Do not invent task progress.
Return plain text only, do not call tools. This summary replaces older conversation;
the public initial instructions and any separately retained recent turns remain available."""


class BusinessBudgetReached(Exception):
    pass


def provider_stop_reason(error, state):
    if state.get("locked") == "budget_exhausted":
        return "provider_budget"
    if state.get("locked") == "budget_exceeded":
        return "provider_upstream_budget_violation"
    return "provider_" + error.code.lower()


def payload(messages, model):
    output_field = "max_tokens" if model.startswith(("claude-", "gemini-")) else "max_completion_tokens"
    return {"model": model, "reasoning_effort": "medium", "messages": messages,
            output_field: 8192, "stream": False, "n": 1}


def estimate_input(proxy, request):
    return len(proxy.encoding.encode(_encode(request).decode(), disallowed_special=())) + 256 + 16 * len(request["messages"])


def compact_split(proxy, initial, history):
    # Retain complete call/result turns only, never an oversized fixed tail.
    for size in (4, 2, 0):
        tail = history[-size:] if size else []
        prefix = history[:-size] if size else history
        if prefix and estimate_input(proxy, payload(initial + tail, proxy.model)) < 64000:
            break
    from .compact_history import bounded_history_prefix
    def measure(messages):
        return estimate_input(proxy, payload(messages + [{'role': 'user', 'content': SUMMARY}], proxy.model)) + 8192
    bounded, remaining = bounded_history_prefix(initial, prefix, measure,
        limit=getattr(proxy, 'compact_request_limit', 480000) - 2048)
    return bounded, remaining + tail


def parse_action(text):
    value = strict_json(text)
    if not isinstance(value, dict):
        raise ValueError("Object required")
    if value.get("type") == "final":
        if set(value) != {"type", "text"} or not isinstance(value["text"], str):
            raise ValueError("Invalid final envelope")
        return value
    if (set(value) != {"type", "actions"} or value["type"] != "actions"
            or not isinstance(value["actions"], list) or not 1 <= len(value["actions"]) <= 32):
        raise ValueError("Invalid actions envelope")
    for action in value["actions"]:
        if (not isinstance(action, dict) or not {"name", "arguments"} <= action.keys()
                or action.keys() - {"name", "arguments", "skill_run_id"}
                or not isinstance(action["name"], str) or not isinstance(action["arguments"], dict)
                or "skill_run_id" in action and not isinstance(action["skill_run_id"], str)):
            raise ValueError("Invalid action envelope")
    return value


def parse_actor_action(text, *, action_protocol=STRICT_ACTION_PROTOCOL):
    """Dispatch the explicitly selected framing contract; never repair arguments."""
    if action_protocol not in ACTION_PROTOCOLS:
        raise ValueError("Unknown action protocol")
    if action_protocol == action_protocol_v6.PROTOCOL:
        return action_protocol_v6.parse(text)
    if action_protocol == action_protocol_v7.PROTOCOL:
        return action_protocol_v7.parse(text)
    if action_protocol == action_protocol_v8.PROTOCOL:
        return action_protocol_v8.parse(text)
    if action_protocol == action_protocol_v5.PROTOCOL:
        return action_protocol_v5.parse(text)
    if action_protocol == action_protocol_v4.PROTOCOL:
        return action_protocol_v4.parse(text)
    if action_protocol == CURRENT_ACTION_PROTOCOL:
        return action_protocol_v3.parse(text)
    try:
        return parse_action(text), 0
    except (ValueError, TypeError, RecursionError):
        if action_protocol == STRICT_ACTION_PROTOCOL or not isinstance(text, str):
            raise
    start = text.find("{")
    prefix = text[:start].strip() if start >= 0 else ""
    if (not prefix or len(prefix) > MAX_ACTION_PREFIX_CHARS
            or any(char in prefix for char in '{}[]`"')):
        raise ValueError("Expected plain leading prose followed by one actions object")
    # strict_json consumes the entire remainder, including rejecting suffixes,
    # duplicate keys, extra JSON values, non-finite numbers and excessive depth.
    action = parse_action(text[start:])
    if action["type"] != "actions":
        raise ValueError("A final envelope must be standalone JSON")
    return action, start


def public_messages(task, tools, *, action_protocol=STRICT_ACTION_PROTOCOL, expected_tools=None):
    if action_protocol not in ACTION_PROTOCOLS:
        raise ValueError("Unknown action protocol")
    fieldops = (task.get("format") == "skillhorizon.operations.v1"
                and task.get("config", {}).get("family") == "staged_maintenance")
    expected = set(expected_tools) if expected_tools is not None else FIELDOPS_ALLOWED if fieldops else ALLOWED
    if {t["name"] for t in tools} != expected:
        raise ValueError("Unexpected tools in controlled business profile")
    initial = []
    for item in task["messages"]:
        if item.get("role") not in {"user", "system", "developer"} or not isinstance(item.get("content"), str):
            raise ValueError("Expected zero-prefix public messages")
        initial.append({"role": item["role"], "content": item["content"]})
    system = SYSTEM
    if action_protocol == CURRENT_ACTION_PROTOCOL:
        system += action_protocol_v3.INSTRUCTIONS
    elif action_protocol == action_protocol_v4.PROTOCOL:
        system += action_protocol_v4.INSTRUCTIONS
    elif action_protocol == action_protocol_v5.PROTOCOL:
        system += action_protocol_v5.INSTRUCTIONS
    elif action_protocol == action_protocol_v6.PROTOCOL:
        system += action_protocol_v6.INSTRUCTIONS
    elif action_protocol == action_protocol_v7.PROTOCOL:
        system += action_protocol_v7.INSTRUCTIONS
    elif action_protocol == action_protocol_v8.PROTOCOL:
        system += action_protocol_v8.INSTRUCTIONS
    if action_protocol in TEXT_ACTION_PROTOCOLS:
        system += action_protocol_v3.tool_instructions(tools)
    if action_protocol == PREFIX_ACTION_PROTOCOL:
        system += ("The actions object is the sole executable instruction. Prefer JSON only. "
                   "For compatibility, up to 1024 characters of leading plain prose may precede "
                   "one complete actions object; that prose is non-operative commentary. "
                   "No trailing prose, multiple JSON values, code fences, or prefixed final envelopes.\n")
    return [{"role": "system", "content": system + "\nPublic tools:\n" +
             json.dumps(tools, ensure_ascii=False, sort_keys=True)}, *initial]


def dispatch(client, action, tools, *, detailed_errors=False, allowed_tools=None):
    definitions = {t["name"]: t for t in tools}
    name = action["name"]
    if name not in (DISPATCH_ALLOWED if allowed_tools is None else allowed_tools) or name not in definitions:
        error = {"code": "UNKNOWN_TOOL"}
        if detailed_errors:
            error.update(path="$/name", message="Use a tool name from the public tools catalogue.")
        return {"ok": False, "error": error}
    invalid = next(Draft202012Validator(definitions[name]["parameters"]).iter_errors(action["arguments"]), None)
    if invalid:
        error = {"code": "PUBLIC_SCHEMA"}
        if detailed_errors:
            error = action_protocol_v3.schema_detail(invalid, path="$/arguments")
            error["tool"] = name
            error["executed"] = False
            if name in {"execute_step", "run_skill_script"}:
                error["hint"] = action_protocol_v3.tool_argument_hint(definitions[name])
        return {"ok": False, "error": error}
    metadata = {"skill_run_id": action["skill_run_id"]} if "skill_run_id" in action else {}
    try:
        return client.call(name, action["arguments"], **metadata)
    except HTTPError as error:
        if error.execution_uncertain or error.declaration_uncertain:
            raise
        if error.status == 429 and strict_json(error.body) == {"error": "BUSINESS_CALL_BUDGET"}:
            raise BusinessBudgetReached from None
        if error.status not in {400, 409}:
            raise
        return {"ok": False, "gateway_error": strict_json(error.body)}


def response_text(raw, *, allow_native_actions=False):
    value = strict_json(raw)
    choice = value["choices"][0]
    message = choice["message"]
    if allow_native_actions and message.get("tool_calls"):
        actions = []
        for call in message["tool_calls"]:
            if call.get("type") != "function":
                raise ValueError("Unsupported native call type")
            function = call["function"]
            actions.append({"name": function["name"], "arguments": strict_json(function["arguments"])})
        text = json.dumps({"type": "actions", "actions": actions}, ensure_ascii=False)
        parse_action(text)
        if choice["finish_reason"] not in {"tool_calls", "stop"}:
            return text, choice["finish_reason"]
        return text, "stop"
    if message.get("tool_calls") or message.get("function_call"):
        raise RuntimeError("Unexpected provider-side tool calls")
    text = message.get("content")
    return text if isinstance(text, str) else "", choice["finish_reason"]


def run(export, root, proxy, *, max_calls, turn_limit=None, action_protocol=STRICT_ACTION_PROTOCOL,
        allow_native_actions=False, continuation=None, attempt_policy=None,
        gateway_factory=None, public_task=None, tool_profile=None, wall_time_seconds=None,
        defer_scoring=False, final_confirmation=None, time_accounting=LEGACY):
    """A missing turn_limit leaves only explicit provider/call budgets, never 256."""
    # Frozen callers can still provide their recorded finite historical limit.
    if wall_time_seconds is not None and (isinstance(wall_time_seconds, bool)
            or not isinstance(wall_time_seconds, (int, float))
            or not math.isfinite(wall_time_seconds) or wall_time_seconds <= 0):
        raise ValueError("Expected a positive finite task wall time or None")
    export, root = Path(export), Path(root)
    if action_protocol not in ACTION_PROTOCOLS:
        raise ValueError("Unknown action protocol")
    if action_protocol in TEXT_ACTION_PROTOCOLS and allow_native_actions:
        raise ValueError("The v3 text-JSON protocol uses the same non-native interface for all models")
    framing = {p.PROTOCOL: p for p in (action_protocol_v3, action_protocol_v4,
                                      action_protocol_v5, action_protocol_v6,
                                      action_protocol_v7, action_protocol_v8)}.get(action_protocol, action_protocol_v4)
    parse_errors = ((action_protocol_v3.ProtocolError, ResponseIssue)
                    if action_protocol in TEXT_ACTION_PROTOCOLS
                    else (ValueError, TypeError, RecursionError, ResponseIssue))
    root.mkdir(parents=True, exist_ok=True)
    if (root / "STARTED.json").exists():
        raise ValueError("No blind replay or overwrite of an existing actor run")
    if continuation and 'zero_b_recovery' in continuation and not isinstance(continuation['zero_b_recovery'], dict):
        raise ValueError('Invalid explicit zero-B checkpoint')
    recovery = ZeroBRecovery(root, state=continuation.get('zero_b_recovery') if continuation else None,
        source=(Path(continuation['business_source']).parent
                if continuation and 'zero_b_recovery' not in continuation else None))
    task = public_task if public_task is not None else json.loads((export / "environment/business/task/task.json").read_text())
    write_record(root / "STARTED.json", {"time": time.time(), "model": proxy.model,
        "harness": HARNESSES[action_protocol],
        "action_protocol": action_protocol, "allow_agent_workspace": False,
        "response_diagnostics_version": RESPONSE_DIAGNOSTICS_VERSION,
        "zero_b_recovery": recovery.summary(),
        "allow_native_actions": allow_native_actions,
        "allow_arbitrary_code_execution": False, "actor_turn_limit": turn_limit,
        "compact_at": COMPACT_AT, "max_calls": max_calls, "wall_time_seconds": wall_time_seconds,
        "time_accounting": time_accounting})
    began = time.monotonic()
    execution_clock = getattr(proxy, 'execution_clock', None)
    if time_accounting == EXECUTION_TIME_MODE:
        if execution_clock is None:
            prior_reader = getattr(proxy, 'continuation_elapsed', None)
            execution_clock = ExecutionClock(root, wall_time_seconds,
                prior_seconds=prior_reader() if callable(prior_reader) else 0)
            proxy.execution_clock = execution_clock
        elif execution_clock.limit != wall_time_seconds:
            raise ValueError('Execution clock and task time limit disagree')
        proxy.continuation_elapsed = execution_clock.elapsed
        if attempt_policy:
            attempt_policy.time_accounting = time_accounting
            attempt_policy.elapsed_reader = execution_clock.elapsed
    elif time_accounting != LEGACY or execution_clock is not None:
        raise ValueError('Execution clock and time accounting mode disagree')
    def elapsed():
        clock = getattr(proxy, "continuation_elapsed", None)
        return clock() if callable(clock) else time.monotonic() - began
    def time_exhausted():
        return wall_time_seconds is not None and elapsed() >= wall_time_seconds
    gateway, stop, decisions, compactions, dispatched = None, "interrupted", 0, 0, 0
    errors, peak, business, exception, claims = 0, 0, None, None, None
    framing_recoveries = 0
    diagnostics = ResponseDiagnostics()
    protocol_streak = 0
    try:
        if gateway_factory is not None:
            options = {}
            if continuation:
                if not getattr(gateway_factory, 'supports_explicit_continuation', False):
                    raise ValueError('Custom gateway does not support explicit continuation')
                options['continuation_source'] = continuation['business_source']
            gateway = gateway_factory(export, root / 'business', max_calls=max_calls, **options)
            client = gateway.client
        else:
            gateway = PrivateGateway(export / "environment/business", root / "business", max_calls=max_calls,
                                     continuation_source=continuation.get("business_source") if continuation else None)
            client = Client(gateway.url, skill_protocol=DECLARATION_PROTOCOL)
        tools = client.tools()["tools"]
        initial = public_messages(task, tools, action_protocol=action_protocol, expected_tools=tool_profile)
        if attempt_policy and hasattr(attempt_policy, 'public_instructions'):
            initial[0]['content'] += '\n' + attempt_policy.public_instructions()
        if getattr(proxy, 'output_limit_feedback', False):
            from .output_recovery import INSTRUCTIONS
            initial[0]['content'] += ("\nBefore choosing final, compare the user's goal with the public results you have\n"
                'actually observed. State remaining work or an observed blocker accurately.\n' + INSTRUCTIONS)
        history = []
        if continuation:
            initial, history = continuation["initial"], continuation["history"]
            if not any(ZERO_B_VERSION in m.get('content', '') for m in initial + history):
                raise ValueError('Continuation is missing public zero-B policy; explicit migration required')
            decisions, compactions = continuation["actor_decisions"], continuation["compact_calls"]
            dispatched, errors = continuation["dispatch_attempts"], continuation["protocol_errors"]
            peak = continuation.get("peak_estimated_input_tokens", 0)
            if recovery.state['last_actor'] != decisions:
                raise ValueError('Recovery state and actor checkpoint differ')
        elif final_confirmation is not None:
            initial[0]['content'] += '\n' + getattr(final_confirmation, 'initial_instructions',
                ('Stopping requires confirmation: a final requests to end the task. '
                'Submit final again without executing intervening tool actions to confirm and end it. '
                'If you execute tools instead, continue working; your next final will require confirmation again. '
                'Invalid responses neither confirm nor cancel a pending confirmation. Budgets and time do not reset.'))
        if not continuation:
            initial[0]['content'] += '\n' + recovery.instructions()
        def finish_actor_turn():
            recovery.finish_actor()
            notice = recovery.warning()
            if notice:
                history.append({'role': 'user', 'content': notice})
        protocol_streak = continuation.get('protocol_streak', 0) if continuation else 0
        compact_output_feedback = recovery.state['compact_feedback']
        while turn_limit is None or decisions < turn_limit:
            if attempt_policy and dispatched >= max_calls:
                stop = "tool_attempt_budget"
                break
            if time_exhausted():
                stop = "wall_time_limit"
                break
            if recovery.stop_reason():
                stop = recovery.stop_reason()
                break
            messages = initial + history
            request = payload(messages, proxy.model)
            estimate = estimate_input(proxy, request)
            peak = max(peak, estimate)
            role = "actor"
            prefix, tail = compact_split(proxy, initial, history) if estimate >= COMPACT_AT else ([], [])
            restored_compaction = continuation.pop('resume_compaction_prefix_count', None) if continuation else None
            if restored_compaction is not None:
                prefix,tail=history[:restored_compaction],history[restored_compaction:]
            if prefix and (estimate >= COMPACT_AT or restored_compaction is not None):
                role = "compact"
                request = payload(initial + prefix + [{"role": "user", "content": SUMMARY}], proxy.model)
                if compact_output_feedback:
                    request['messages'].append({'role': 'user', 'content': compact_output_feedback})
            if role == "actor" and getattr(proxy, "actor_response_format", None):
                request["response_format"] = proxy.actor_response_format
                peak = max(peak, estimate_input(proxy, request))
            if estimate_input(proxy, request) + 8192 > 480000:
                stop = "context_capacity"
                break
            sequence = decisions + compactions + 1
            directory = root / "responses" / f"{sequence:05d}"
            directory.mkdir(parents=True)
            write_record(directory / "request.json", request)
            if continuation and continuation.get("expected_next_request") is not None:
                if request != continuation.pop("expected_next_request"):
                    raise ValueError("Continuation public context differs from saved boundary")
            set_deadline = getattr(proxy, "set_execution_deadline", None)
            if callable(set_deadline):
                set_deadline(None if wall_time_seconds is None else
                             time.monotonic() + max(0, wall_time_seconds - elapsed()))
            request_prefix = continuation.get('request_id_prefix', '') if continuation else ''
            try:
                raw = proxy.forward(request, request_id=f"{request_prefix}{role}-{sequence:05d}")
            except OutputLimitFeedback as rejection:
                response = strict_json(rejection.raw)
                write_record(directory / 'rejected_output.json', response)
                if time_exhausted():
                    stop = 'wall_time_limit'
                    break
                feedback = rejection.public_feedback(response, compact=role == 'compact')
                event = dict(role=role, output_limit_feedback=True,
                    elapsed_seconds=elapsed(), dispatch_attempts=dispatched,
                    reported_completion_tokens=rejection.usage['completion_tokens'],
                    limit=rejection.maximum, actions_executed=False)
                if role == 'compact':
                    compactions += 1
                    recovery.compact_rejected(sequence, feedback)
                    compact_output_feedback = feedback
                    event['original_history_preserved'] = True
                    choice = response['choices'][0]
                    issue = response_issue(choice['message'].get('content'), choice['finish_reason'], response)
                    detail = issue.detail if issue else dict(code='OUTPUT_LIMIT', category='output_limit')
                    if issue:
                        event['original_history_preserved'] = True
                    diagnostics.record(detail, role)
                    event['response_rejection'] = detail
                else:
                    decisions += 1
                    recovery.actor_response(decisions, output_rejected=True)
                    choice = response['choices'][0]
                    text = choice['message'].get('content') or ''
                    finish = choice['finish_reason']
                    protocol_error = None
                    parsed_action = None
                    try:
                        message = response['choices'][0]['message']
                        validate_actor_response(text, finish, response)
                        parsed_action, _ = parse_actor_action(text, action_protocol=action_protocol)
                    except parse_errors as error:
                        protocol_error, instruction = rejection_feedback(error, framing)
                        feedback += '\n' + instruction
                    if protocol_error:
                        errors += 1
                        protocol_streak += 1
                        event.update(protocol_error=True,
                                     model_protocol_error=protocol_error['category'] == 'model_protocol',
                                     protocol_error_detail=protocol_error)
                    detail = protocol_error or dict(code='OUTPUT_LIMIT', category='output_limit')
                    diagnostics.record(detail, role)
                    event['response_rejection'] = detail
                    event['unusable_streak'] = protocol_streak
                    # Rejected content is visible only as a rejected assistant turn;
                    # it is never dispatched or adopted as a final confirmation.
                    history.extend([{'role':'assistant', 'content':text},
                                    {'role':'user', 'content':feedback}])
                    event['decision'] = decisions
                    finish_actor_turn()
                event['zero_b_recovery'] = recovery.summary()
                append_event(root / 'events.jsonl', event)
                write_record(directory / 'OUTPUT_RECOVERY.json', event)
                write_record(root / 'PROGRESS.json', dict(actor_decisions=decisions,
                    compact_calls=compactions, dispatch_attempts=dispatched,
                    peak_estimated_input_tokens=peak))
                if role == 'actor' and attempt_policy and protocol_streak >= attempt_policy.protocol_recovery_limit:
                    stop = 'protocol_recovery_exhausted'
                    break
                continue
            response = strict_json(raw)
            write_record(directory / "response.json", response)
            if time_exhausted():
                stop = "wall_time_limit"
                break
            choice = response['choices'][0]
            issue = response_issue(choice['message'].get('content'), choice['finish_reason'], response,
                                   allow_native=allow_native_actions and role == 'actor')
            try:
                message = response["choices"][0]["message"]
                if issue:
                    text = message.get("content") if isinstance(message.get("content"), str) else ""
                    finish = choice['finish_reason']
                else:
                    text, finish = response_text(raw, allow_native_actions=allow_native_actions and role == "actor")
            except (ValueError, TypeError, KeyError, RecursionError):
                if not allow_native_actions or role != "actor":
                    raise
                text, finish = "", "invalid_native_actions"
            if role == "compact":
                compactions += 1
                if issue:
                    diagnostics.record(issue.detail, role)
                    compact_output_feedback = ('The summary was unusable: ' + issue.detail['code'] + '. '
                        'Return a concise, complete plain-text summary of the same visible history '
                        'within the original output limit. No partial summary has been adopted.')
                    recovery.compact_rejected(sequence, compact_output_feedback)
                    append_event(root / 'events.jsonl', dict(role=role, response_rejection=issue.detail,
                        actions_executed=False, original_history_preserved=True,
                        zero_b_recovery=recovery.summary()))
                    continue
                new_history = [{"role": "user", "content": "Summary of earlier visible conversation:\n" + text}] + tail
                after = estimate_input(proxy, payload(initial + new_history, proxy.model))
                if after >= estimate:
                    stop = "compact_insufficient_reduction"
                    break
                if time_exhausted():
                    stop = 'wall_time_limit'
                    break
                history = new_history
                compact_output_feedback = None
                recovery.compact_adopted()
                append_event(root / "events.jsonl", {"role": role, "before": estimate, "after": after})
                continue
            decisions += 1
            recovery.actor_response(decisions, output_rejected=finish == 'length')
            history.append({"role": "assistant", "content": text})
            try:
                if issue:
                    raise issue
                if finish != "stop":
                    raise ValueError("Incomplete actor output")
                action, prefix_chars = parse_actor_action(text, action_protocol=action_protocol)
            except parse_errors as error:
                errors += 1
                protocol_streak += 1
                detail, message = rejection_feedback(error, framing)
                diagnostics.record(detail, role)
                event = {"role": role, "decision": decisions,
                         "protocol_error": True,
                         "model_protocol_error": detail['category'] == 'model_protocol',
                         "protocol_error_detail": detail, "response_rejection": detail,
                         "unusable_streak": protocol_streak, "actions_executed": False}
                history.append({"role": "user", "content": message})
                append_event(root / "events.jsonl", event)
                finish_actor_turn()
                if attempt_policy and protocol_streak >= attempt_policy.protocol_recovery_limit:
                    stop = "protocol_recovery_exhausted"
                    break
                continue
            if time_exhausted():
                stop = 'wall_time_limit'
                break
            protocol_streak = 0
            if action_protocol in {action_protocol_v4.PROTOCOL, action_protocol_v5.PROTOCOL,
                                   action_protocol_v6.PROTOCOL, action_protocol_v7.PROTOCOL,
                                   action_protocol_v8.PROTOCOL}:
                history[-1]["content"] = framing.history_content(text, action)
            if prefix_chars:
                framing_recoveries += 1
                append_event(root / "events.jsonl", {"role": role, "decision": decisions,
                    "action_protocol": action_protocol,
                    "action_framing": ("normalized_closed_think_actions_suffix_v8"
                        if action_protocol == action_protocol_v8.PROTOCOL else "normalized_closed_think_final_v7"
                        if action_protocol == action_protocol_v7.PROTOCOL else "normalized_complete_actions_v6"
                        if action_protocol == action_protocol_v6.PROTOCOL
                        else "normalized_single_intent" if action_protocol == action_protocol_v5.PROTOCOL
                        else "leading_prose"),
                    "prefix_chars": prefix_chars})
            if action["type"] == "final":
                if final_confirmation is not None:
                    confirmed = final_confirmation.on_final()
                    append_event(root / "events.jsonl", {"role": role, "decision": decisions,
                        "final_confirmation": final_confirmation.summary()})
                    if not confirmed:
                        from .final_confirmation import CONFIRMATION
                        history.append({"role": "user", "content": getattr(final_confirmation, 'prompt', CONFIRMATION)})
                        finish_actor_turn()
                        continue
                recovery.finish_actor()
                stop = "actor_final"
                break
            results = []
            for call in action["actions"]:
                if time_exhausted():
                    stop = "wall_time_limit"
                    break
                if attempt_policy:
                    if dispatched >= max_calls:
                        stop = "tool_attempt_budget"
                        break
                    violation = attempt_policy.before(call)
                    if violation:
                        stop = "local_failure_limit"
                        append_event(root / "events.jsonl", {"role": role, "decision": decisions,
                            "rejected_before_dispatch": call, "local_failure_limit": violation,
                            "prefix_dispatch_attempts": dispatched})
                        break
                charged = attempt_policy.charge(call) if attempt_policy and hasattr(attempt_policy, 'charge') else 1
                if final_confirmation is not None:
                    if hasattr(final_confirmation, 'on_tool_attempt'):
                        final_confirmation.on_tool_attempt(call, charged)
                    else:
                        final_confirmation.on_tool_executed()
                dispatched += charged
                if charged:
                    recovery.charged_attempt()
                result = dispatch(client, call, tools, detailed_errors=action_protocol in TEXT_ACTION_PROTOCOLS,
                                  allowed_tools=tool_profile)
                feedback = None
                if attempt_policy:
                    feedback = attempt_policy.observe(call, result)
                results.append({"name": call["name"], "result": result})
                append_event(root / "events.jsonl", {"role": role, "decision": decisions,
                    "call": call, "result": result, "dispatch_attempts": dispatched,
                    "elapsed_seconds": elapsed()})
                if time_exhausted():
                    stop = 'wall_time_limit'
                    break
                if isinstance(feedback, dict) and feedback.get('terminate'):
                    stop = 'local_failure_limit'
                    append_event(root / 'events.jsonl', {'role':role, 'decision':decisions,
                        'local_failure_limit':feedback, 'prefix_dispatch_attempts':dispatched})
                    break
                if isinstance(feedback, dict) and feedback.get('failed'):
                    break
            if stop in {"wall_time_limit", "tool_attempt_budget", "local_failure_limit"}:
                break
            history.append({"role": "user", "content": "Public tool results:\n" +
                            json.dumps(results, ensure_ascii=False, sort_keys=True) +
                            (f'\nExecuted {len(results)} of {len(action["actions"])} submitted actions. '
                             'The remaining suffix was not attempted. Decide new actions from this feedback.'
                             if len(results)<len(action['actions']) else '') +
                            (f'\nTool attempts used: {dispatched}/{max_calls}.' if attempt_policy else '')})
            finish_actor_turn()
            write_record(root / "PROGRESS.json", {"actor_decisions": decisions, "compact_calls": compactions,
                "dispatch_attempts": dispatched, "peak_estimated_input_tokens": peak,
                "zero_b_recovery": recovery.summary()})
        else:
            stop = "actor_turn_limit"
    except BusinessBudgetReached:
        stop = "business_call_budget"
    except GuardError as error:
        stop = "wall_time_limit" if time_exhausted() else provider_stop_reason(error, proxy.snapshot())
    except BaseException as error:
        stop = "execution_error"
        exception = type(error).__name__
        # Stack locations are sufficient for runner diagnosis; omit messages,
        # source lines and locals that could contain public or private payloads.
        write_record(root / 'RUNTIME_ERROR.json', dict(exception=exception,
            stack=[dict(file=f.filename, line=f.lineno, function=f.name)
                   for f in traceback.extract_tb(error.__traceback__)]))
    finally:
        recovery.finish_actor()
        if execution_clock is not None:
            execution_clock.finish()
        if defer_scoring:
            write_record(root / "EXECUTION_BOUNDARY.json", {
                "stop_reason": stop, "exception": exception,
                "execution_elapsed_seconds": elapsed(), "dispatch_attempts": dispatched,
                "actor_decisions": decisions, "compact_calls": compactions,
                "protocol_errors": errors, "peak_estimated_input_tokens": peak,
                "response_diagnostics": diagnostics.summary(),
                "unusable_response_streak": protocol_streak,
                "zero_b_recovery": recovery.summary(),
                "protocol_errors_definition": LEGACY_COUNT_DEFINITION,
                "execution_policy": attempt_policy.summary() if attempt_policy else None,
                "final_confirmation": final_confirmation.summary() if final_confirmation else None,
                "provider": proxy.snapshot()})
        if gateway is not None:
            try:
                if not defer_scoring:
                    business = gateway.seal_and_score()
                    claims = gateway.measure_claims()
            except Exception as error:
                exception = "score:" + type(error).__name__
                # Only PrivateGatewayError has allowlisted, non-secret labels.
                from .private_gateway import PrivateGatewayError
                if isinstance(error, PrivateGatewayError):
                    exception += ":" + str(error)
            finally:
                gateway.close()
        receipt = {"stop_reason": stop, "actor_decisions": decisions, "compact_calls": compactions,
            "action_protocol": action_protocol, "framing_recoveries": framing_recoveries,
            "dispatch_attempts": dispatched, "protocol_errors": errors, "business": business, "claims": claims,
            "response_diagnostics": diagnostics.summary(),
            "unusable_response_streak": protocol_streak,
            "zero_b_recovery": recovery.summary(),
            "protocol_errors_definition": LEGACY_COUNT_DEFINITION,
            "exception": exception, "peak_estimated_input_tokens": peak,
            "elapsed_seconds": elapsed(), "wall_time_seconds": wall_time_seconds, "provider": proxy.snapshot(),
            "capabilities": {"allow_agent_workspace": False, "allow_arbitrary_code_execution": False}}
        if execution_clock is not None:
            receipt.update(time_accounting=time_accounting, timing=execution_clock.snapshot(),
                           execution_elapsed_seconds=execution_clock.elapsed())
        if attempt_policy:
            receipt["execution_policy"] = attempt_policy.summary()
        if final_confirmation is not None:
            receipt["final_confirmation"] = final_confirmation.summary()
        write_record(root / "RESULT.json", receipt)
    return receipt
