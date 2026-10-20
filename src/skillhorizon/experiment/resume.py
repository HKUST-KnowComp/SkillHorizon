"""Resume a validated request-only infrastructure stop without reexecuting tools."""

import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from ..util import load_json, load_jsonl, write_json
from .config import RunConfig
from .execution import ExecutionPolicy, decide_calls
from .messages import append_turn, parse_response, request_payload
from .context import prepare_request, apply_summary, summary_config
from .provider_contract import budget_usage, retryable_configuration_response
from .rate_limits import is_rate_limit, retry_schedule
from .transport import (FixtureTransport, HttpTransport, retryable_prompt_rejection,
                        retryable_transport_failure)


def resume_kind(error):
    if is_rate_limit(error):
        return "rate_limit"
    if retryable_transport_failure(error):
        return "transport"
    raise ValueError("Only a confirmed HTTP 429 or allowlisted request transport failure can resume")


def resumed_config(config, error):
    return replace(config, **{resume_kind(error) + "_retries": 3})


def linkage_resumed_config(config):
    if config.retry_unrequested_linkage or config.provider_contract_retries != 3:
        raise ValueError("Linkage continuation requires previously disabled linkage retries and three existing retries")
    return replace(config, retry_unrequested_linkage=True)


def expected_parent_stop(kind):
    return {"infrastructure_budget": "infrastructure_budget",
            "linkage_contract": "provider_contract_error"}.get(kind, "provider_error")


def infrastructure_resumed_config(config, task):
    """Explicitly remove a lifetime infra allowance without adding model turns."""
    if config.max_infrastructure_failures is None:
        raise ValueError("No finite infrastructure allowance to amend")
    updated = replace(config, max_infrastructure_failures=None, max_provider_requests=None)
    requests = ExecutionPolicy.for_task(updated, task).maximum_requests
    return replace(updated, max_provider_requests=max(config.max_provider_requests or 0, requests))


def claim_resume(parent_root, output, run):
    """One atomic continuation claim beside the immutable canonical parent."""
    parent_root, output = Path(parent_root).resolve(), Path(output).resolve()
    claim = parent_root.parent / f".resume-{run['resume']['parent_run_id']}.json"
    try:
        with claim.open("x", encoding="utf-8") as handle:
            json.dump({"parent_run_id": run["resume"]["parent_run_id"],
                       "child_run_id": run["run_id"], "parent": str(parent_root),
                       "output": str(output)}, handle, sort_keys=True)
    except FileExistsError as exc:
        raise ValueError("Parent already has a continuation claim; refusing duplicate API spending") from exc
    return claim


def parent_config(root, run, config):
    metadata = run.get("resume")
    if metadata is None:
        return config
    parent = load_json(root / "PARENT_RUN.json")
    result = load_json(root / "PARENT_RESULT.json")
    old = RunConfig.from_record(parent["config"])
    kind = metadata.get("retry_kind", "rate_limit")
    expected_stop = expected_parent_stop(kind)
    if (parent.get("resume") or parent["run_id"] != metadata["parent_run_id"]
            or parent["run_id"] == run["run_id"] or parent["stop_reason"] != expected_stop
            or parent["transport"] != run["transport"] or result.get("valid") is not True
            or result.get("outcome") != expected_stop
            or type(metadata["parent_turn_count"]) is not int or metadata["parent_turn_count"] < 1):
        raise ValueError("Invalid infrastructure resume parent")
    if kind == "infrastructure_budget":
        if config != infrastructure_resumed_config(old, load_json(root / "task.json")):
            raise ValueError("Infra amendment may only remove its lifetime cap and derive retry request headroom")
        return old
    if kind == "linkage_contract":
        if config != linkage_resumed_config(old):
            raise ValueError("Linkage continuation cannot change model, context or budgets")
        return old
    if kind not in {"rate_limit", "transport"}:
        raise ValueError("Unknown resume retry kind")
    field = kind + "_retries"
    if ({k: v for k, v in old.to_dict().items() if k != field} !=
            {k: v for k, v in config.to_dict().items() if k != field}
            or getattr(old, field) != 0 or getattr(config, field) != 3):
        raise ValueError("Resume may only enable three previously disabled retries; original settings and budgets must remain fixed")
    return old


def activate_resume(policy, config, metadata, event, raw=None):
    kind = metadata.get("retry_kind", "rate_limit")
    if (event["turn"] != metadata["parent_turn_count"]
            or event.get("response_accepted") is not False or event["results"] or event["feedback"]):
        raise ValueError("Resume must follow the parent's terminal unexecuted request failure")
    if kind == "linkage_contract":
        request_config = summary_config(config) if event.get("role") == "compaction" else config
        conflicts = event.get("provider_check", {}).get("configuration", {}).get("conflicts", [])
        if (metadata.get("retry_schedule") is not None or event["status"] != "provider_contract_error"
                or "previous_response_id" not in conflicts or not raw
                or not retryable_configuration_response(raw, request_config, event["provider_check"])):
            raise ValueError("Parent linkage conflict is not eligible for unchanged-input retries")
        policy.config = config
        terminal, _ = policy.advance(event["turn"], event["status"], [], [], None, False,
                                    retryable_configuration=True, response_accepted=False,
                                    role=event.get("role", "actor"))
        if terminal is not None or not policy.configuration_retry_pending:
            raise ValueError("Original request/token/consecutive-retry budget does not permit continuation")
        return None
    if kind == "infrastructure_budget":
        if metadata.get("retry_schedule") is not None:
            raise ValueError("Infra amendment cannot invent a wait schedule")
        configuration = bool(raw) and retryable_configuration_response(raw, config, event.get("provider_check", {}))
        error = event.get("error", {})
        rate = event["status"] == "provider_error" and is_rate_limit(error)
        schedule = retry_schedule(error, policy.unchanged_request_retries + 1,
                                  event["rate_limit_observed_at"]) if rate else None
        policy.config = config
        policy.maximum_requests = config.max_provider_requests
        terminal, _ = policy.advance(event["turn"], event["status"], [], [], None, False,
                                    retryable_configuration=configuration,
                                    retryable_rejection=retryable_prompt_rejection(error),
                                    retryable_transport=retryable_transport_failure(error),
                                    retryable_rate_limit=schedule is not None, response_accepted=False,
                                    role=event.get("role", "actor"))
        if terminal is not None or not any((policy.configuration_retry_pending, policy.prompt_rejection_retry_pending,
                                           policy.transport_retry_pending, policy.rate_limit_retry_pending)):
            raise ValueError("Remaining execution/cost/consecutive-retry budget does not permit resumption")
        return schedule if policy.rate_limit_retry_pending else None
    if event["status"] != "provider_error" or resume_kind(event.get("error")) != kind:
        raise ValueError("Resume must follow the parent's terminal unexecuted request failure")
    schedule = None
    if kind == "rate_limit":
        schedule = retry_schedule(event["error"], policy.unchanged_request_retries + 1,
                                  metadata["retry_schedule"]["observed_at"])
        if schedule is None or schedule != metadata["retry_schedule"]:
            raise ValueError("Resume delay differs from Retry-After/backoff")
    elif metadata.get("retry_schedule") is not None:
        raise ValueError("Transport resumption must not invent a rate-limit wait")
    policy.config = config
    terminal, _ = policy.advance(event["turn"], "provider_error", [], [], None, False,
                                retryable_rate_limit=kind == "rate_limit", retryable_transport=kind == "transport",
                                role=event.get("role", "actor"))
    if terminal is not None or not getattr(policy, kind + "_retry_pending"):
        raise ValueError("Original request/token/retry budget does not permit resumption")
    return schedule


def restore_state(root, task, run, config, *, initialize_schedule=False):
    policy = ExecutionPolicy.for_task(parent_config(root, run, config), task)
    messages = deepcopy(task["messages"])
    seen = {m["call_id"] for m in messages if m.get("type") == "function_call"}
    events = load_jsonl(root / "events.jsonl")
    terminal = None
    for event in events:
        if terminal is not None:
            raise ValueError("Parent contains requests after termination")
        calls, usage, raw = [], None, None
        role, payload, compact_plan, capacity_stop = prepare_request(
            policy.config, messages, task["tools"], task, policy.compaction_turns)
        if capacity_stop or role != event.get("role", "actor"):
            raise ValueError("Recorded context role differs during recovery")
        request_config = summary_config(policy.config) if role == "compaction" else policy.config
        if event["status"] != "provider_error":
            raw = load_json(root / "turns" / f'{event["turn"]:04d}' / "response.json")
            calls, usage = parse_response(raw, config.model)
        if event["status"] == "completed":
            if role == "compaction":
                messages, record = apply_summary(config, messages, task["tools"], raw, compact_plan)
                if record != event["compaction"]:
                    raise ValueError("Recorded compaction differs during recovery")
            else:
                decide_calls(calls, seen, task["tools"], event["turn"])
                append_turn(messages, raw, event["results"], event["feedback"], event.get("feedback_text"))
        rate_schedule = None
        if (event["status"] == "provider_error" and is_rate_limit(event.get("error"))
                and policy.config.rate_limit_retries
                and policy.unchanged_request_retries < policy.config.rate_limit_retries):
            rate_schedule = retry_schedule(event["error"], policy.unchanged_request_retries + 1,
                                           event["rate_limit_observed_at"])
        terminal, _ = policy.advance(event["turn"], event["status"], calls, event["results"],
            budget_usage(usage, event.get("provider_check"), policy.config), False,
            retryable_configuration=bool(raw) and retryable_configuration_response(raw, request_config, event["provider_check"]),
            retryable_rejection=retryable_prompt_rejection(event.get("error", {})),
            retryable_rate_limit=rate_schedule is not None,
            retryable_transport=retryable_transport_failure(event.get("error", {})),
            response_accepted=event["response_accepted"], role=role)
    expected_stop = expected_parent_stop(run["resume"].get("retry_kind"))
    if terminal != expected_stop or len(events) != run["resume"]["parent_turn_count"]:
        raise ValueError("Parent is not a complete terminal provider-error trace")
    if initialize_schedule and run["resume"].get("retry_kind", "rate_limit") == "rate_limit":
        number = policy.unchanged_request_retries + 1
        if number > config.rate_limit_retries:
            raise ValueError("Unchanged-request retry budget is exhausted")
        run["resume"]["retry_schedule"] = retry_schedule(events[-1]["error"], number,
            run["resume"]["retry_schedule"]["observed_at"])
        if run["resume"]["retry_schedule"] is None:
            raise ValueError("Retry-After exceeds the automatic wait ceiling")
    schedule = activate_resume(policy, config, run["resume"], events[-1], raw)
    failed_request = load_json(root / "turns" / f'{len(events):04d}' / "request.json")
    if prepare_request(config, messages, task["tools"], task, policy.compaction_turns)[1] != failed_request:
        raise ValueError("Resumed request differs from the failed request")
    pending = (root / "resume_wait.json", schedule) if schedule is not None else None
    return messages, policy, seen, len(events) + 1, pending


def resume_task(parent_root, output, config, transport):
    from .scoring import score_run
    from .runner import _run
    parent_root, output = Path(parent_root).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(parent_root):
        raise ValueError("Resume output must be fresh and separate from its parent")
    if not isinstance(transport, (FixtureTransport, HttpTransport)):
        raise TypeError("Use a supported transport")
    if isinstance(transport, HttpTransport) and (transport.description["retries"] != config.retries
            or transport.description["timeout_seconds"] != config.timeout_seconds):
        raise ValueError("Resume transport differs from configuration")
    parent_run, recorded = load_json(parent_root / "run.json"), load_json(parent_root / "RESULT.json")
    with TemporaryDirectory(prefix="skillhorizon-resume-audit-") as tmp:
        checked = Path(tmp) / "parent"
        shutil.copytree(parent_root, checked)
        verified = score_run(checked)
    if (not verified["valid"] or verified["outcome"] not in {"provider_error", "infrastructure_budget", "provider_contract_error"}
            or recorded.get("outcome") != verified["outcome"] or not recorded["valid"]
            or recorded["state"] != verified["state"]):
        raise ValueError("Parent execution did not pass independent replay")
    events = load_jsonl(parent_root / "events.jsonl")
    if not events:
        raise ValueError("Cannot resume an empty trace")
    kind = ("infrastructure_budget" if verified["outcome"] == "infrastructure_budget" else
            "linkage_contract" if verified["outcome"] == "provider_contract_error" else resume_kind(events[-1].get("error")))
    if parent_run.get("resume"):
        raise ValueError("A resumed attempt cannot be branched again to reset exhausted retries")
    run = {**deepcopy(parent_run), "run_id": str(uuid4()), "config": config.to_dict(),
           "stop_reason": "interrupted", "transport": transport.description,
           "resume": {"parent_run_id": parent_run["run_id"], "parent_turn_count": len(events),
                      "source_path": str(parent_root), "retry_kind": kind,
                      "retry_schedule": retry_schedule(events[-1]["error"], 1, time.time()) if kind == "rate_limit" else None}}
    if kind == "rate_limit" and run["resume"]["retry_schedule"] is None:
        raise ValueError("Retry-After exceeds the automatic wait ceiling")
    # Validate before creating the execution branch; no credential or tool is used here.
    with TemporaryDirectory(prefix="skillhorizon-resume-state-") as tmp:
        staged = Path(tmp) / "run"
        shutil.copytree(parent_root, staged)
        write_json(staged / "PARENT_RUN.json", parent_run)
        write_json(staged / "PARENT_RESULT.json", recorded)
        restore_state(staged, load_json(staged / "task.json"), run, config, initialize_schedule=True)
    claim_resume(parent_root, output, run)
    shutil.copytree(parent_root, output)
    (output / "RESULT.json").rename(output / "PARENT_RESULT.json")
    write_json(output / "PARENT_RUN.json", parent_run)
    write_json(output / "run.json", run)
    task = load_json(output / "task.json")
    restored = restore_state(output, task, run, config)
    return asyncio.run(_run(Path(parent_run["task_root"]), output, task, config, transport, run, restored=restored))
