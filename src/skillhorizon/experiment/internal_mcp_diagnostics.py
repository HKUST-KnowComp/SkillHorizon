"""Offline observational-skill diagnostics, without DataOps-specific score assumptions."""

from collections import Counter
import json

from ..util import canonical_json, load_json, load_jsonl, write_json
from .context import input_size
from .environment_score import evaluate_environment
from .native_diagnostics import object_mentions, mentioned, compaction_mentions
from .benchmark_measurements import measure_episode


def diagnose(bundle, run, output, task):
    from .managed_session import require_world_quiescent
    require_world_quiescent(bundle)
    from .workspace_bundle import split_tools, workspace_usage
    tools, workspace_records = split_tools(bundle, load_jsonl(run / "tools.jsonl"))
    direct_tools = tools
    from .execution_evidence import business_observations
    tools = business_observations(bundle, run, direct_tools)
    attempts = load_jsonl(run / "attempts.jsonl")
    schema_rejections = load_jsonl(run / "tool_rejections.jsonl")
    initial = load_jsonl(bundle / "checkpoint/model_skill_actions.jsonl")
    executed = load_jsonl(bundle / "runtime/model_skill_actions.jsonl")[len(initial):]
    if len(tools) != len(executed) or any((a["name"], a["arguments"], a["output"]) !=
            (b["skill_id"], b["arguments"], b["result"]) for a, b in zip(tools, executed)):
        raise ValueError("SDK tools differ from the independent environment action log")
    effects = evaluate_environment(task, bundle / "checkpoint", bundle / "runtime")
    if not effects["valid"]:
        raise ValueError("Invalid internal MCP execution evidence")
    requests, lengths = {}, []
    for event in attempts:
        request = load_json(run / "turns" / f'{event["turn"]:04d}' / "request.json")
        lengths.append({"turn": event["turn"], "role": event["role"], "accepted": event["accepted"],
                        "local_input_tokens": input_size(request),
                        "provider_input_tokens": (event.get("usage") or {}).get("input_tokens"),
                        "provider_output_tokens": (event.get("usage") or {}).get("output_tokens"),
                        "reported_total_tokens": event["reported_tokens"]})
        if event["accepted"]:
            requests[event["turn"]] = request
    seen, reuse = {}, []
    for index, item in enumerate(task["messages"]):
        if item.get("type") == "function_call_output":
            for identifier in object_mentions(json.loads(item["output"])):
                seen[identifier] = {"origin": "checkpoint_history", "message_index": index}
    errors, runs, active = [], {}, None
    # Prefix attribution is reconstructed, but only new calls enter online error counters.
    for action in initial:
        result = action["result"]
        data = result.get("data", {})
        if result.get("status") == "success":
            if action["skill_id"] == "begin_skill":
                active = data["run_id"]
                runs[active] = {"skill_id": action["arguments"]["skill_id"], "begin_turn": None, "from_checkpoint": True, "claimed_complete": False, "errors": []}
            elif action["skill_id"] == "select_skill_run":
                active = action["arguments"]["run_id"]
            elif action["skill_id"] == "finish_skill":
                identifier = action["arguments"]["run_id"]
                if identifier in runs:
                    runs[identifier]["claimed_complete"] = True
                if active == identifier:
                    active = None
    for row in tools:
        result, arguments = row["output"], row["arguments"]
        text = canonical_json(requests[row["turn"]]["input"])
        for identifier in object_mentions(arguments) if row.get("audience", "model") == "model" else ():
            if identifier in seen:
                reuse.append({"turn": row["turn"], "id": identifier, "last_observed": seen[identifier],
                              "producer_in_same_unobserved_batch": seen[identifier].get("turn") == row["turn"],
                              "active_context_id_mentioned": mentioned(identifier, text)})
        data = result.get("data", {})
        if result.get("status") == "success":
            if row["name"] == "begin_skill":
                active = data["run_id"]
                runs[active] = {"skill_id": arguments["skill_id"], "begin_turn": row["turn"], "from_checkpoint": False, "claimed_complete": False, "errors": []}
            elif row["name"] == "select_skill_run":
                active = arguments["run_id"]
            elif row["name"] == "finish_skill":
                identifier = arguments["run_id"]
                if identifier in runs:
                    runs[identifier]["claimed_complete"] = True
                if active == identifier:
                    active = None
        else:
            error = {"turn": row["turn"], "tool_index": row.get("tool_index"),
                     "audience": row.get("audience", "model"), "parent_id": row.get("parent_id"), "child_seq": row.get("child_seq"),
                     "tool": row["name"], "run_id": active, "code": result.get("message", "TOOL_ERROR")}
            errors.append(error)
            if active in runs:
                runs[active]["errors"].append(error)
        for identifier in object_mentions(result) if row.get("audience", "model") == "model" else ():
            seen[identifier] = {"origin": "online_tool_result", "turn": row["turn"]}
    compactions = load_jsonl(run / "compactions.jsonl")
    common = measure_episode(task, initial, tools, effects, attempts, load_json(run / "SUMMARY.json")["stop_reason"])
    from integrations.benchmark_suite.horizon import episode_horizon
    common["horizon"] = episode_horizon(task, bundle)
    report = {"revision": "internal_mcp_native_process_v1", "visibility": "offline_only",
        "benchmark_measurements": common,
        "environment_id": task["config"]["environment_id"], "environment_format": task["environment_format"], "skill_protocol": "observational_v1",
        "instance_identity": task.get("instance_identity"), "scenario": task["config"].get("scenario"),
        "business_verification": effects, "tool_calls": len(tools),
        "workspace_tool_calls": sum(r["name"] not in {"python_execute", "ptc_execute"} for r in workspace_records),
        "execution_tool_calls": sum(r["name"] in {"python_execute", "ptc_execute"} for r in workspace_records),
        "total_tool_calls": len(direct_tools) + len(workspace_records),
        "business_observation_audiences": dict(Counter(r.get("audience", "model") for r in tools)),
        "business_observation_scope": "Business calls include program children; total_tool_calls counts top-level SDK returns only. Program results are not model exposure.",
        "workspace_usage": workspace_usage(workspace_records),
        "tool_counts": dict(Counter(r["name"] for r in tools)),
        "model_tool_errors": errors, "public_schema_rejections": schema_rejections,
        "public_protocol_rejections": load_jsonl(run / "protocol_rejections.jsonl"),
        "terminal_protocol_failures": load_jsonl(run / "model_action_failures.jsonl"),
        "case_progress": {"supported": False, "reason": "Internal MCP has no independently defined case-level progress contract"},
        "skill_runs": runs, "context_lengths": lengths, "reuse_mentions": reuse,
        "selection": {"begin_attempts": sum(r["name"] == "begin_skill" for r in tools), "unique_optimal_next_skill_accuracy": None,
                      "note": "Observed selection and claimed completion are not independently established skill correctness."},
        "compactions": [compaction_mentions(requests[r["turn"]], r) for r in compactions],
        "post_compaction_execution": [{"turn": c["turn"], "successful_tool_calls": sum(r["turn"] > c["turn"] and r["output"].get("status") == "success" for r in tools)} for c in compactions],
        "successful_script_calls": sum(r["name"] == "run_skill_script" and r["output"].get("status") == "success" for r in tools),
        "infrastructure": {"rejected_responses": sum(not e["accepted"] for e in attempts), "unknown_usage_attempts": sum(e["reported_tokens"] is None for e in attempts)},
        "reuse_note": "Checkpoint and direct model-visible business outputs only; program results are excluded. stdout/file exposure is not counted here. Not required-memory coverage or attention.", "litm_inference": "not_established"}
    from .execution_bundle import execution_accounting
    report.update(execution_accounting(bundle))
    write_json(output, report)
    protocol_errors = sum(r["output"]["error"]["code"] == "ARGUMENT_JSON" for r in report["public_protocol_rejections"])
    return {"output": str(output), "tool_calls": len(tools),
            "model_errors": len(errors) + len(schema_rejections) + protocol_errors + len(report["terminal_protocol_failures"]),
            "business_success": effects["task_success"], "compactions": len(compactions)}
