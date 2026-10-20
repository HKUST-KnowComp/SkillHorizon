"""Recompute project outcomes from public actions and business state."""

from copy import deepcopy
from pathlib import Path
import shutil
import tempfile

from ..util import load_json, load_jsonl, write_json
from .config import RunConfig
from .messages import accountable_usage, append_turn, parse_response, request_payload
from .provider_contract import checked_response_status, summarize_provider_checks, retryable_configuration_response, budget_usage
from .project_runtime import project_runtime
from .execution import ExecutionPolicy, continuation_feedback, decide_calls
from .skill_metrics import skill_execution_measurements
from .binding_diagnostics import dataflow_call_diagnostics, summarize_diagnostics
from .recovery_metrics import RecoveryMeasurements, maintenance_measurements
from .rate_limits import is_rate_limit, retry_schedule, validate_wait
from .resume import parent_config, activate_resume
from ..dataflow import FORMAT as DATAFLOW_FORMAT
from ..dataflow.support import environment_support
from ..dataflow.verification import verify_release_content
from .context_metrics import request_observation, binding_positions
from .tasks import validate_project_history
from .transport import retryable_prompt_rejection, retryable_transport_failure
from ..benchmark_spec import observable_diagnostics, assigned_split, world_group, artifact_reuse_measurements

ANALYSIS_REVISION = "compaction_480k_200k_v1"


def _write_result(root, report):
    report["analysis_revision"] = ANALYSIS_REVISION
    destination = root / "RESULT.json"
    if destination.exists() and load_json(destination).get("analysis_revision") != ANALYSIS_REVISION:
        destination = root / f"ANALYSIS_{ANALYSIS_REVISION}.json"
    write_json(destination, report)


def score_project_run(root: Path):
    try:
        return _score_project_run(root)
    except (OSError, ValueError, TypeError, KeyError, IndexError) as exc:
        run = load_json(root / "run.json")
        report = {"version": 4, "task_id": load_json(root / "task.json").get("task_id"),
            "evidence_source": run["transport"]["kind"], "valid": False,
            "task_success": False, "outcome": "invalid_run", "errors": [f"Incomplete or inconsistent execution evidence: {exc}"],
            "provider_checks": summarize_provider_checks([]), "litm_inference": "not_established",
            "provider_requests": None, "input_tokens": None, "output_tokens": None}
        _write_result(root, report)
        return report


def _score_project_run(root: Path):
    from .context import prepare_request, apply_summary, summary_config
    run, task = load_json(root / "run.json"), load_json(root / "task.json")
    # Missing historical settings must not inherit newly enabled retries.
    config = RunConfig.from_record({"canonical_history": False, "prompt_rejection_retries": 0,
                         "provider_contract_retries": 0,
                         "provider_contract_retry_scope": "configuration_only", **run["config"]})
    events = load_jsonl(root / "events.jsonl")
    actions = load_jsonl(root / "runtime/model_skill_actions.jsonl")
    prefix = task["checkpoint_action_count"]
    index = prefix
    messages = deepcopy(task["messages"])
    source = run["transport"]["kind"]
    errors, progress, usages = [], [], []
    error_codes, per_skill = {}, {}
    call_records, context_records, diagnostics = [], [], []
    dataflow = task["format"] == DATAFLOW_FORMAT
    recovery = RecoveryMeasurements(prefix) if dataflow else None
    if dataflow and task.get("environment_support") != environment_support(task["config"]["notice_policy"], task["config"].get("workspace_profile")):
        raise ValueError("Declared environment support differs from the implemented DataOps surface")
    calls_count = executed = successes = no_calls = completed_skills = 0
    policy = ExecutionPolicy.for_task(parent_config(root, run, config), task)
    seen = {m["call_id"] for m in messages if m.get("type") == "function_call"}
    terminal = None
    pending_wait = None
    resume = run.get("resume")
    resume_retry_flags = {}
    if resume and resume["parent_turn_count"] > len(events):
        raise ValueError("Resume parent prefix is incomplete")
    with tempfile.TemporaryDirectory(prefix="skillhorizon-score-") as tmp:
        replay = Path(tmp) / "runtime"
        shutil.copytree(root / "checkpoint", replay)
        reference_prefix = load_jsonl(replay / "model_skill_actions.jsonl")
        validate_project_history(task, reference_prefix)
        if actions[:prefix] != reference_prefix or prefix != len(reference_prefix):
            errors.append("Checkpoint actions changed")
        runtime = project_runtime(task, replay)
        if dataflow and runtime.project.get("notice_policy") != task["config"]["notice_policy"]:
            raise ValueError("Checkpoint source-change notice policy differs from task configuration")
        initial_state = runtime.score()
        initial_maintenance = maintenance_measurements(runtime.project)
        skill_names = task["skill_ids"]
        for turn, event in enumerate(events, 1):
            directory = root / "turns" / f"{turn:04d}"
            try:
                if resume and turn == resume["parent_turn_count"] + 1:
                    parent_result = load_json(root / "PARENT_RESULT.json")
                    if (terminal != parent_result["outcome"] or runtime.score() != parent_result["state"]
                            or executed != parent_result["executed_tool_calls"]):
                        raise ValueError("Resumed boundary differs from the original parent outcome")
                    previous = events[turn - 2]
                    previous_raw = (load_json(root / "turns" / f'{turn - 1:04d}' / "response.json")
                                    if previous["status"] != "provider_error" else None)
                    schedule = activate_resume(policy, config, resume, previous, previous_raw)
                    if resume.get("retry_kind") in {"infrastructure_budget", "linkage_contract"}:
                        resume_retry_flags = {
                            "configuration_retry": policy.configuration_retry_pending,
                            "prompt_rejection_retry": policy.prompt_rejection_retry_pending,
                            "transport_retry": policy.transport_retry_pending,
                            "rate_limit_retry": schedule if policy.rate_limit_retry_pending else None}
                    if schedule is not None:
                        validate_wait(root / "resume_wait.json", schedule, source == "external")
                    terminal = None
                if pending_wait is not None:
                    validate_wait(*pending_wait, source == "external")
                    pending_wait = None
                if terminal is not None or turn > policy.maximum_requests:
                    raise ValueError("Provider turn recorded after the execution policy stopped")
                role, payload, compact_plan, capacity_stop = prepare_request(
                    policy.config, messages, task["tools"], task, policy.compaction_turns)
                request_config = summary_config(policy.config) if role == "compaction" else policy.config
                if capacity_stop or event.get("role", "actor") != role:
                    raise ValueError("Request role differs from the context policy")
                if event["turn"] != turn or load_json(directory / "request.json") != payload:
                    raise ValueError("Actual request differs from configuration/history")
                observation = request_observation(payload, runtime.read_project(), runtime.score())
                if event.get("request_observation") != observation:
                    raise ValueError("Request context/work measurement differs from public history and state")
                context_row = {"turn": turn, "role": role, "local_public_input_tokens": observation["local_public_input_tokens"],
                    "opaque_reasoning_items_not_tokenized": observation["opaque_reasoning_items_not_tokenized"],
                    "business_state": observation["business_state"], "provider_input_tokens": None, "provider_output_tokens": None}
                context_records.append(context_row)
                if event["transport"].get("kind") != source:
                    raise ValueError("Transport source changed")
                if source == "external" and event["transport"].get("base_url") != run["transport"].get("base_url"):
                    raise ValueError("Provider route changed")
                if event["status"] == "provider_error":
                    if event.get("response_accepted") is not False:
                        raise ValueError("Provider failure cannot be an accepted response")
                    if (load_json(directory / "error.json") != event["error"] or event["results"]
                            or (directory / "response.json").exists() or event["feedback"] or event.get("configuration_retry")):
                        raise ValueError("Provider error evidence mismatch")
                    rate_schedule = None
                    if is_rate_limit(event["error"]) and policy.config.rate_limit_retries:
                        number = policy.unchanged_request_retries + 1
                        if number <= policy.config.rate_limit_retries:
                            rate_schedule = retry_schedule(event["error"], number, event["rate_limit_observed_at"])
                    terminal, _ = policy.advance(turn, "provider_error", [], [], None, False,
                        retryable_rejection=retryable_prompt_rejection(event["error"]),
                        retryable_rate_limit=rate_schedule is not None,
                        retryable_transport=retryable_transport_failure(event["error"]), role=role)
                    if event.get("transport_retry", False) is not policy.transport_retry_pending:
                        raise ValueError("Transport retry differs from the bounded execution policy")
                    if event.get("prompt_rejection_retry", False) is not policy.prompt_rejection_retry_pending:
                        raise ValueError("Prompt rejection retry differs from the bounded execution policy")
                    expected_schedule = rate_schedule if policy.rate_limit_retry_pending else None
                    if event.get("rate_limit_retry") != expected_schedule:
                        raise ValueError("Rate-limit retry differs from the bounded execution policy")
                    if expected_schedule is not None:
                        pending_wait = (directory / "retry_wait.json", expected_schedule)
                    continue
                if event.get("rate_limit_retry") or event.get("rate_limit_observed_at"):
                    raise ValueError("Only an HTTP 429 can schedule rate-limit waiting")
                if event.get("prompt_rejection_retry"):
                    raise ValueError("Only a provider prompt rejection can schedule its retry")
                if event.get("transport_retry"):
                    raise ValueError("Only a request transport failure can schedule its retry")
                raw = load_json(directory / "response.json")
                if event["status"] == "invalid_response":
                    try:
                        parse_response(raw, config.model)
                    except (ValueError, TypeError):
                        if event["results"] or event["feedback"] or event.get("configuration_retry"):
                            raise ValueError("Invalid response cannot contain tool execution or feedback")
                        if event.get("response_accepted") is not False:
                            raise ValueError("Invalid response cannot be accepted")
                        usage = accountable_usage(raw)
                        if usage is not None:
                            usages.append(usage)
                        terminal, _ = policy.advance(turn, "invalid_response", [], [], usage, False, role=role)
                        continue
                    raise ValueError("Invalid-response label disagrees with response")
                calls, usage = parse_response(raw, config.model)
                expected_bindings = binding_positions(observation, calls)
                if event.get("input_bindings") != expected_bindings:
                    raise ValueError("Public input-binding positions differ from actual request")
                context_row.update(provider_input_tokens=usage["input_tokens"] if source == "external" else None,
                                   provider_output_tokens=usage["output_tokens"] if source == "external" else None,
                                   input_bindings=expected_bindings)
                usages.append(usage)
                expected_status = checked_response_status(run, event, payload, raw, request_config)
                if role == "compaction" and expected_status == "completed":
                    if event["results"] or event["feedback"] or event.get("configuration_retry"):
                        raise ValueError("Compaction executed tools or added continuation feedback")
                    try:
                        updated, record = apply_summary(config, messages, task["tools"], raw, compact_plan)
                    except ValueError as exc:
                        if event["status"] != "compaction_error" or event.get("summary_error") != str(exc) or event["response_accepted"]:
                            raise ValueError("Invalid compaction evidence") from exc
                        terminal, _ = policy.advance(turn, "compaction_error", [], [],
                            budget_usage(usage, event["provider_check"], config), False, role=role)
                        continue
                    if event["status"] != "completed" or not event["response_accepted"] or event.get("compaction") != record:
                        raise ValueError("Summary differs from the recorded context transition")
                    messages = updated
                    terminal, _ = policy.advance(turn, "completed", [], [],
                        budget_usage(usage, event["provider_check"], config), False, role=role)
                    continue
                usable = expected_status == "completed"
                if event.get("response_accepted") is not usable:
                    raise ValueError("Response acceptance differs from raw provider validation")
                if event["status"] == "local_error" and usable:
                    if not event.get("local_error") or not any(r["output"].get("error", {}).get("code") == "LOCAL_TOOL_ERROR" for r in event["results"]):
                        raise ValueError("Local error has no invocation-failure evidence")
                    terminal, _ = policy.advance(turn, "local_error", calls, event["results"],
                        budget_usage(usage, event["provider_check"], config), False, response_accepted=True, role=role)
                    errors.append(f"Turn {turn}: MCP failure has unverified execution effects; response acceptance and usage retained")
                    continue
                if event["status"] != expected_status:
                    raise ValueError("Response status disagrees with tokens/completion")
                if not usable:
                    if event["results"] or event["feedback"]:
                        raise ValueError("Unusable response executed tools")
                    terminal, _ = policy.advance(turn, expected_status, calls, [], budget_usage(usage, event["provider_check"], config), False,
                        retryable_configuration=retryable_configuration_response(raw, request_config, event["provider_check"]), role=role)
                    if event.get("configuration_retry", False) is not policy.configuration_retry_pending:
                        raise ValueError("Configuration retry differs from the bounded execution policy")
                    continue
                if event.get("configuration_retry"):
                    raise ValueError("Usable response cannot be discarded as a configuration retry")
                if len(calls) != len(event["results"]):
                    raise ValueError("Call/result count mismatch")
                no_calls += int(not calls)
                decisions = decide_calls(calls, seen, task["tools"], turn,
                    allow_multiple=config.parallel_tool_calls,
                    remaining_calls=None if config.max_tool_calls is None else config.max_tool_calls - policy.tool_calls)
                for call, result, decision in zip(calls, event["results"], decisions):
                    calls_count += 1
                    if (call["call_id"] != result["call_id"] or type(result["executed"]) is not bool
                            or result["transcript_call_id"] != decision["transcript_call_id"]
                            or not isinstance(result["output"], dict)):
                        raise ValueError("Call identity, execution flag or transcript identity mismatch")
                    stats = per_skill.setdefault(call["name"], {"attempts": 0, "successes": 0})
                    stats["attempts"] += 1
                    diagnostic = dataflow_call_diagnostics(runtime, call["name"], call["arguments"]) if dataflow else None
                    before = deepcopy(runtime.project) if dataflow else None
                    if decision["rejection"] is not None:
                        if result["executed"] or result["committed_node"] is not None or result["output"] != decision["rejection"]:
                            raise ValueError("Rejected tool outcome differs from the public execution policy")
                    else:
                        if not result["executed"]:
                            raise ValueError("Legal tool call was marked unexecuted while returning a tool result")
                        executed += 1
                        expected = runtime.call(call["name"], decision["arguments"])
                        observed = actions[index]
                        replay_action = load_jsonl(replay / "model_skill_actions.jsonl")[-1]
                        if (expected != result["output"] or observed != replay_action
                                or result["committed_node"] != observed["node_id"]):
                            raise ValueError("Tool output or runtime action differs from actual semantic execution")
                        index += 1
                    ok = result["executed"] and result["output"].get("ok") is True
                    if recovery is not None:
                        recovery.observe(before, runtime, call["name"], call["arguments"], result["output"], turn, diagnostic)
                    if diagnostic is not None:
                        diagnostics.append({"turn": turn, "tool": call["name"], "diagnostic": diagnostic,
                                            "operation_succeeded": ok,
                                            "returned_error": result["output"].get("error", {}).get("code")})
                    successes += int(ok)
                    stats["successes"] += int(ok)
                    if ok and (call["name"] == "finish_skill" or call["name"] in skill_names):
                        completed_skills += 1
                    code = result["output"].get("error", {}).get("code")
                    if code:
                        error_codes[code] = error_codes.get(code, 0) + 1
                    call_records.append({"turn": turn, "name": call["name"], "arguments": call["arguments"],
                                         "executed": result["executed"], "output": result["output"]})
                state = runtime.score()
                terminal, feedback = policy.advance(turn, expected_status, calls, event["results"],
                    budget_usage(usage, event["provider_check"], config), state["termination_ready"])
                if (type(event["feedback"]) is not bool or event["feedback"] != feedback
                        or event.get("feedback_text") != (continuation_feedback(task) if feedback else None)):
                    raise ValueError("Continuation feedback differs from the execution policy")
                append_turn(messages, raw, event["results"], event["feedback"], event.get("feedback_text"))
                progress.append({"turn": turn, "completed_cases": state["completed_cases"],
                    "business_success": state["success"], "lifecycle_complete": state["lifecycle_complete"],
                    "termination_ready": state["termination_ready"], "input_tokens": usage["input_tokens"]})
            except (ValueError, TypeError, KeyError, IndexError, OSError) as exc:
                errors.append(f"Turn {turn}: {exc}")
        state = runtime.score()
        if load_json(replay / "project.json") != load_json(root / "runtime/project.json"):
            errors.append("Final project state differs from replay")
        if runtime.snapshot() != project_runtime(task, root / "runtime").snapshot():
            errors.append("Final public source or artifact contents differ from replay")
    if index != len(actions):
        errors.append("Unrecorded runtime actions")
    if run.get("context_stop"):
        _, _, _, reason = prepare_request(config, messages, task["tools"], task, policy.compaction_turns)
        if terminal is not None or run["context_stop"] != {"before_turn": len(events) + 1, "reason": reason} or reason is None:
            errors.append("Unverified context-policy stop")
        else:
            terminal = reason
    if (root / "active_context.json").exists() and load_json(root / "active_context.json") != messages:
        errors.append("Saved active context differs from recorded transitions")
    derived_stop = terminal or "interrupted"
    if run["stop_reason"] != derived_stop:
        errors.append(f"Termination differs from execution evidence: expected {derived_stop}")
    actual_turns = {p.name for p in (root / "turns").iterdir()} if (root / "turns").exists() else set()
    if actual_turns != {f"{i:04d}" for i in range(1, len(events) + 1)}:
        errors.append("Missing or unrecorded provider turn")
    independent_content = verify_release_content(root / "runtime") if dataflow else None
    independent_maintenance = None
    if task["config"].get("workspace_profile") == "release_maintenance_v2":
        from ..dataflow.maintenance_verification import verify_maintenance
        independent_maintenance = verify_maintenance(root / "runtime", root / "checkpoint")
        if state["success"] and not independent_maintenance["passed"]:
            errors.append("Independent maintenance content verification failed")
    if dataflow and state["success"] and independent_content["passed"] is not True:
        errors.append("Runtime success disagrees with independent publication-content verification")
    valid = not errors and source in {"external", "fixture"}
    outcome = "invalid_run" if not valid else derived_stop
    if outcome == "success" and not state["termination_ready"]:
        outcome = "state_verification_failed"
    retry_events = events
    if resume_retry_flags:
        retry_events = deepcopy(events)
        retry_events[resume["parent_turn_count"] - 1].update(resume_retry_flags)
    cache_counts = [(u.get("input_tokens_details") or {}).get("cached_tokens") for u in usages]
    report = {"version": 6, "task_id": task["task_id"], "evidence_source": source,
        "valid": valid, "errors": errors, "task_success": valid and state["success"], "outcome": outcome,
        "lifecycle_complete": valid and state["lifecycle_complete"],
        "termination_ready": valid and state["termination_ready"],
        "completion_semantics": "task_success verifies business acceptance/publication; termination waits for its own committed run to close; unrelated open runs are a separate lifecycle metric",
        "state": state, "provider_requests": len(events) if source == "external" else 0,
        "accepted_model_turns": policy.usable_turns if source == "external" else 0,
        "initial_state": initial_state,
        "completed_cases_change": state["completed_cases"] - initial_state["completed_cases"],
        "fixture_responses": len(events) if source == "fixture" else 0,
        "request_attempts": len(events), "tool_calls": calls_count, "executed_tool_calls": executed,
        "usable_response_turns": policy.usable_turns,
        "unusable_request_attempts": len(events) - policy.usable_turns - policy.compaction_turns,
        "budget_limits": {"usable_turns": policy.maximum_turns, "request_attempts": policy.maximum_requests,
                          "tool_calls": config.max_tool_calls,
                          "infrastructure_failures": config.max_infrastructure_failures},
        "budget_consumed": {"usable_turns": policy.usable_turns, "request_attempts": len(events),
                            "tool_calls": policy.tool_calls,
                            "infrastructure_failures": policy.infrastructure_failures,
                            "resource_truncations": policy.resource_truncations,
                            "environment_failures": policy.environment_failures,
                            "accepted_response_tokens": policy.accepted_tokens,
                            "rejected_response_tokens": policy.rejected_tokens,
                            "resource_truncation_tokens": policy.resource_tokens,
                            "reported_tokens": policy.used_tokens},
        "turn_budget_note": "Only accepted actor responses consume usable turns, including wrong tools and text-only responses. Summaries consume separate compaction slots, request attempts and reported tokens, not business turns. Rejected responses consume infrastructure/request limits and reported tokens. Token thresholds are checked after each response; one in-flight request can cross the threshold. Local tool failures stop without assuming zero side effects.",
        "successful_tool_calls": successes, "no_call_turns": no_calls,
        "completed_skill_runs": completed_skills, "reference_remaining_skills": task["remaining_skill_count"],
        "current_goal_coverage": (state["completed_cases"] + int(state["success"])) / (state["total_cases"] + 1),
        "coverage_note": "Current valid case deliverables plus final handoff; includes prefix work, is not model progress, and can decrease after a business event.",
        "input_tokens": sum(u["input_tokens"] for u in usages) if source == "external" else 0,
        "output_tokens": sum(u["output_tokens"] for u in usages) if source == "external" else 0,
        "initial_input_tokens": usages[0]["input_tokens"] if usages and source == "external" else None,
        "peak_input_tokens": max((u["input_tokens"] for u in usages), default=None) if source == "external" else None,
        "cached_input_tokens": sum(cache_counts) if cache_counts and all(v is not None for v in cache_counts) else None,
        "error_codes": error_codes, "per_tool": per_skill, "progress": progress,
        "execution_mode": task["execution_mode"], "litm_inference": "not_established",
        "derived_stop_reason": derived_stop,
        "evaluation_track": "checkpoint_continuation" if prefix else "full_online",
        "model": config.model, "reasoning_effort": config.reasoning_effort}
    report["context_measurements"] = context_records
    report["input_tokens_by_role"] = {}
    for role in ("actor", "compaction"):
        measured = [r["provider_input_tokens"] for r in context_records
                    if r["role"] == role and r["provider_input_tokens"] is not None]
        report["input_tokens_by_role"][role] = {
            "first": measured[0] if measured else None,
            "peak": max(measured, default=None),
            "sum": sum(measured) if measured else None}
    report["context_policy"] = config.context_policy
    report["compaction"] = {"threshold": config.compact_at, "window": config.context_window,
        "successful_compactions": policy.compaction_turns,
        "summary_failures": policy.compaction_failures,
        "request_attempts": sum(e.get("role") == "compaction" for e in events),
        "reported_tokens": policy.compaction_tokens,
        "transitions": [dict(turn=e["turn"], **e["compaction"]) for e in events if "compaction" in e],
        "measurement_note": "Local active-input estimates exclude opaque reasoning; not provider measurements. Summary text is fallible and no lost-in-the-middle effect is inferred."}
    report["rate_limit_retries_scheduled"] = sum(bool(e.get("rate_limit_retry")) for e in retry_events) + int(bool(resume) and resume.get("retry_kind", "rate_limit") == "rate_limit")
    report["transport_retries_scheduled"] = sum(bool(e.get("transport_retry")) for e in retry_events) + int(bool(resume) and resume.get("retry_kind") == "transport")
    report["transport_retry_requests"] = sum(bool(e.get("transport_retry")) for e in retry_events[:-1]) + int(bool(resume) and resume.get("retry_kind") == "transport" and len(events) > resume["parent_turn_count"])
    if resume:
        report["resumption"] = {"parent_run_id": resume["parent_run_id"],
            "retry_kind": resume.get("retry_kind", "rate_limit"),
            "inherited_provider_requests": resume["parent_turn_count"],
            "new_provider_requests": len(events) - resume["parent_turn_count"],
            "cumulative_provider_requests": len(events),
            "note": "Continuation of one interrupted episode, not an independent new trial; budgets and skill history include the parent."}
        if resume.get("retry_kind") == "infrastructure_budget":
            old = load_json(root / "PARENT_RUN.json")["config"]
            report["resumption"]["budget_amendment"] = {key: {"before": old[key], "after": config.to_dict()[key]}
                for key in ("max_infrastructure_failures", "max_provider_requests")}
        if resume.get("retry_kind") == "linkage_contract":
            report["resumption"]["retry_scope_amendment"] = {"retry_unrequested_linkage": {"before": False, "after": True},
                "budget_changes": {}, "acceptance_policy_changes": {},
                "note": "Rejected response remains discarded; only unchanged-input retry eligibility expands."}
    if initial_maintenance is not None:
        final_maintenance = maintenance_measurements(runtime.project)
        report["maintenance_work"] = {"initial": initial_maintenance, "final": final_maintenance,
            "evaluated_suffix_change": {key: final_maintenance[key] - initial_maintenance[key] for key in
                ("repair_hours", "test_hours", "failed_measurement_hours", "rework_repairs", "replacement_parts_consumed")}}
    if source == "external":
        attempts = [e["transport"].get("transport_attempt_count") if e["status"] != "provider_error" else
                    e.get("error", {}).get("details", {}).get("transport_attempt_count") for e in events]
        report["http_attempt_count"] = sum(attempts) if all(type(n) is int and n > 0 for n in attempts) else None
        report["requests_with_unreported_usage"] = len(events) - len(usages)
        report["budget_accounted_tokens"] = policy.used_tokens
        report["usage_note"] = "Totals include available raw provider usage, including conflicting reports. Failed or retried requests may have unreported remote consumption; these are not a verified invoice."
    report["provider_checks"] = summarize_provider_checks(retry_events)
    report["observable_diagnostics"] = observable_diagnostics(events, outcome)
    report["provider_http_200_responses"] = sum(event.get("transport", {}).get("http_status") == 200 for event in events) if source == "external" else 0
    report["skill_execution"] = skill_execution_measurements(task, actions, call_records)
    report["analysis_revision"] = ANALYSIS_REVISION
    if dataflow:
        report["environment_support"] = environment_support(task["config"]["notice_policy"], task["config"].get("workspace_profile"))
        report["offline_call_diagnostics"] = summarize_diagnostics(diagnostics)
        report["independent_content_verification"] = independent_content
        if independent_maintenance is not None:
            report["independent_maintenance_verification"] = independent_maintenance
            report["completion_semantics"] = "Maintenance verifies dispositions, current publication and preserved consumer objects; termination waits for the final publication run to close. Other open runs are a separate lifecycle metric, as in every domain."
            report["business_recovery_scope"] = "Existing tabular skills only; new decision skills are counted by skill_execution, not tabular binding groups"
            maintenance = runtime.project["maintenance"]
            resolutions = [change["resolution"] for change in maintenance["changes"].values() if change["resolution"]]
            report["release_maintenance"] = {
                "requested_changes": len(maintenance["changes"]), "resolved_changes": len(resolutions),
                "applied_changes": sum(item["choice"] == "apply" for item in resolutions),
                "rejected_changes": sum(item["choice"] == "reject" for item in resolutions),
                "created_decisions": len(maintenance["decisions"]),
                "superseded_decisions": sum(item["superseded_by"] is not None for item in maintenance["decisions"].values()),
                "source_conflict_rejections": error_codes.get("STALE_ASSESSMENT", 0) + error_codes.get("STALE_PLAN", 0),
                "environment_origin_skill_runs_excluded": maintenance["environment_origin_skill_count"],
                "note": "Resolution counts are committed dispositions, not a correctness rate. Independent maintenance verification establishes business correctness; old completed regional audits are not current-policy progress."}
        report["business_recovery"] = recovery.report(runtime.project)
    report["benchmark_revision"] = task["benchmark_revision"]
    report["task_structure"] = task["structure"]
    report["benchmark_scope"] = assigned_split(task)
    report["source_world_group"] = world_group(task)
    report["actual_artifact_reuse"] = artifact_reuse_measurements(runtime.project, actions, prefix, task["messages"])
    _write_result(root, report)
    return report
