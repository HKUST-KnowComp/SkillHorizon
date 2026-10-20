"""Offline diagnostics over native records; no observer feedback reaches the model."""

from collections import Counter
import json
from pathlib import Path
import re

from ..util import canonical_json, load_json, load_jsonl, write_json
from .behavior_diagnostics import maintenance_decisions
from .context import input_size
from .environment_score import evaluate_environment
from .native_process import NativeProcessMeasurements
from .benchmark_measurements import measure_episode


def object_mentions(value):
    result = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if (key == "id" or key.endswith("_id")) and isinstance(item, str) and item:
                result.add(item)
            elif key.endswith("_ids") and isinstance(item, list):
                result.update(x for x in item if isinstance(x, str) and x)
            result.update(object_mentions(item))
    elif isinstance(value, list):
        for item in value:
            result.update(object_mentions(item))
    return result


def mentioned(identifier, text):
    return bool(re.search(r"(?<![\w-])" + re.escape(identifier) + r"(?![\w-])", text))


def compaction_mentions(request, record):
    source = json.loads(request["input"][-1]["content"])["history"]
    known = set()
    for item in source:
        if item.get("type") == "function_call_output":
            try:
                known.update(object_mentions(json.loads(item["output"])))
            except (ValueError, TypeError):
                pass
    retained = sorted(identifier for identifier in known if mentioned(identifier, record["summary_text"]))
    return {"turn": record["turn"], "source_object_mentions": sorted(known),
            "summary_retained_id_mentions": retained, "summary_omitted_id_mentions": sorted(known - set(retained)),
            "fact_fidelity": "not_automatically_established",
            "note": "Literal ID retention is not factual correctness or required-memory coverage. Omitted IDs may be irrelevant or recoverable by normal rereading."}


def compaction_cycles(attempts, compactions, tools):
    accepted = {e["turn"] for e in attempts if e["accepted"] and e["role"] == "actor"}
    previous = 0
    rows = []
    for record in compactions:
        turn = record["turn"]
        between = sum(previous < t < turn for t in accepted)
        rows.append({"turn": turn, "actors_since_previous_compaction": between,
                     "kind": "regrowth" if previous and between else "first_after_growth" if between else "initial_or_adjacent_chunk",
                     "tools_after_compaction": sum(t["turn"] > turn for t in tools),
                     "successful_skill_steps_after_compaction": sum(
                         t["turn"] > turn and t.get("name") in {"execute_step", "run_skill_script"}
                         and t.get("output", {}).get("ok") is True for t in tools)})
        previous = turn
    return {"successful_compactions": len(rows), "cycles": rows,
            "regrowth_compactions": sum(r["kind"] == "regrowth" for r in rows),
            "tools_after_last_compaction": rows[-1]["tools_after_compaction"] if rows else 0,
            "successful_skill_steps_after_last_compaction": rows[-1]["successful_skill_steps_after_compaction"] if rows else 0,
            "multi_cycle_observed": bool(len(rows) >= 2 and any(r["kind"] == "regrowth" for r in rows)
                                         and rows[-1]["tools_after_compaction"]),
            "scope": "Timing evidence only; does not establish factual fidelity, task success or natural growth origin"}


def inspection_coverage(tools):
    produced, covered = {}, {}
    for row in tools:
        out = row["output"]
        if not out.get("ok"):
            continue
        identifier = out.get("artifact_id")
        if identifier and "row_count" in out:
            produced[identifier] = out["row_count"]
            covered.setdefault(identifier, set()).update(range(len(out.get("rows", []))))
        identifier = out.get("table_id") if row["name"] == "read_table" else None
        if identifier in produced:
            covered[identifier].update(range(out["offset"], out["offset"] + len(out["rows"])))
    rows = [{"artifact_id": key, "rows": size, "covered_rows": len(covered[key]),
             "complete": covered[key] == set(range(size))} for key, size in produced.items()]
    return {"artifacts": rows, "all_produced_artifacts_covered": bool(rows) and all(r["complete"] for r in rows),
            "note": "Public row exposure, including creation previews; not evidence of comprehension or necessary reading"}


def diagnose_native(bundle, run, output):
    from .managed_session import require_world_quiescent
    require_world_quiescent(bundle)
    bundle, run, output = Path(bundle), Path(run), Path(output)
    if output.exists() or output.resolve().is_relative_to(run.resolve()) or output.resolve().is_relative_to(bundle.resolve()):
        raise ValueError("Native diagnostic overlay must be fresh and outside evidence/bundle")
    task = load_json(bundle / "private_task.json")
    from .internal_mcp import FORMAT as INTERNAL_FORMAT
    if task["format"] == INTERNAL_FORMAT:
        from .internal_mcp_diagnostics import diagnose
        return diagnose(bundle, run, output, task)
    from .workspace_bundle import split_tools, workspace_usage
    tools, workspace_records = split_tools(bundle, load_jsonl(run / "tools.jsonl"))
    direct_tools = tools
    from .execution_evidence import business_observations
    tools = business_observations(bundle, run, direct_tools)
    schema_rejections = load_jsonl(run / "tool_rejections.jsonl")
    initial = load_jsonl(bundle / "checkpoint/model_skill_actions.jsonl")
    executed = load_jsonl(bundle / "runtime/model_skill_actions.jsonl")[len(initial):]
    if len(tools) != len(executed) or any((a["name"], a["arguments"], a["output"]) !=
            (b["skill_id"], b["arguments"], b["result"]) for a, b in zip(tools, executed)):
        raise ValueError("SDK tool log disagrees with independent environment trace")
    attempts = load_jsonl(run / "attempts.jsonl")
    process = NativeProcessMeasurements(task, initial, tools, attempts)
    from ..dataflow.runtime import DataflowRuntime
    from ..fieldops.runtime import OperationsRuntime
    runtime_type = DataflowRuntime if process.dataflow else OperationsRuntime
    process.completion.observe(runtime_type(bundle / "checkpoint"), "checkpoint", 0, 0)
    effects = evaluate_environment(task, bundle / "checkpoint", bundle / "runtime", observer=process.observe)
    if not effects["valid"]:
        raise ValueError("Cannot diagnose invalid native effects")
    lengths, requests = [], {}
    for event in attempts:
        payload = load_json(run / "turns" / f'{event["turn"]:04d}' / "request.json")
        lengths.append({"turn": event["turn"], "role": event["role"], "accepted": event["accepted"],
                        "local_input_tokens": input_size(payload),
                        "provider_input_tokens": (event.get("usage") or {}).get("input_tokens"),
                        "provider_output_tokens": (event.get("usage") or {}).get("output_tokens"),
                        "reported_total_tokens": event["reported_tokens"]})
        if event["accepted"]:
            requests[event["turn"]] = payload
    seen, reuse, errors, runs = {}, [], [], {}
    for row in tools:
        args, result = row["arguments"], row["output"]
        active = canonical_json(requests[row["turn"]]["input"])
        for identifier in object_mentions(args) if row.get("audience", "model") == "model" else ():
            if identifier in seen:
                reuse.append({"turn": row["turn"], "id": identifier,
                              "last_tool_output_mention": seen[identifier],
                              "producer_in_same_unobserved_batch": seen[identifier] == row["turn"],
                              "active_context_id_mentioned": mentioned(identifier, active)})
        if row["name"] == "begin_skill" and result.get("ok"):
            runs[result["run_id"]] = {"skill_id": args["skill_id"], "begin_turn": row["turn"], "errors": [], "finished": False}
        run_id = args.get("run_id")
        if result.get("ok") is False:
            errors.append({"turn": row["turn"], "tool_index": row.get("tool_index"),
                           "audience": row.get("audience", "model"), "parent_id": row.get("parent_id"), "child_seq": row.get("child_seq"),
                           "tool": row["name"], "run_id": run_id, "code": result["error"]["code"]})
            if run_id in runs:
                runs[run_id]["errors"].append(result["error"]["code"])
        if row["name"] == "finish_skill" and result.get("ok") and run_id in runs:
            runs[run_id]["finished"] = True
        for identifier in object_mentions(result) if row.get("audience", "model") == "model" else ():
            seen[identifier] = row["turn"]
    final, checkpoint = load_json(bundle / "runtime/project.json"), load_json(bundle / "checkpoint/project.json")
    decisions = maintenance_decisions(checkpoint, final, [{**r, "executed": True} for r in tools]) if final.get("workspace_profile") == "release_maintenance_v2" else None
    common = measure_episode(task, initial, tools, effects, attempts, load_json(run / "SUMMARY.json")["stop_reason"])
    from integrations.benchmark_suite.horizon import episode_horizon
    common["horizon"] = episode_horizon(task, bundle)
    report = {"revision": "native_process_v1", "visibility": "offline_only", "business_verification": effects,
              "benchmark_measurements": common,
              "tool_calls": len(tools), "workspace_tool_calls": sum(r["name"] not in {"python_execute", "ptc_execute"} for r in workspace_records),
              "execution_tool_calls": sum(r["name"] in {"python_execute", "ptc_execute"} for r in workspace_records),
              "workspace_usage": workspace_usage(workspace_records),
              "total_tool_calls": len(direct_tools) + len(workspace_records),
              "business_observation_audiences": dict(Counter(r.get("audience", "model") for r in tools)),
              "business_observation_scope": "Business calls include program children; total_tool_calls counts top-level SDK returns only. Program results are not model exposure.",
              "tool_counts": dict(Counter(r["name"] for r in tools)),
              "model_tool_errors": errors, "public_schema_rejections": schema_rejections,
              "public_protocol_rejections": load_jsonl(run / "protocol_rejections.jsonl"),
              "terminal_protocol_failures": load_jsonl(run / "model_action_failures.jsonl"),
              "skill_runs": runs, "business_decisions": decisions,
              "selection": {"begin_attempts": sum(r["name"] == "begin_skill" for r in tools),
                            "unique_optimal_next_skill_accuracy": None,
                            "note": "Legality is not utility. Alternative schedules are allowed; unassessed choices remain unknown."},
              "context_lengths": lengths, "reuse_mentions": reuse,
              "reuse_note": "Direct model-visible business outputs only; program results are excluded. IDs exposed through stdout or workspace files are not counted by this limited counter. Not required-memory coverage, understanding or attention.",
              "compactions": [compaction_mentions(requests[r["turn"]], r) for r in load_jsonl(run / "compactions.jsonl")],
              "compaction_execution": compaction_cycles(attempts, load_jsonl(run / "compactions.jsonl"), tools),
              "business_process": process.report(final, effects, load_json(run / "run.json")["stop_reason"]),
              "inspection": inspection_coverage(direct_tools) if task.get("agent_observation_policy") == "inspect_new_artifact_rows_v1" else None,
              "infrastructure": {"rejected_responses": sum(not e["accepted"] for e in attempts),
                                 "unknown_usage_attempts": sum(e["reported_tokens"] is None for e in attempts)},
              "litm_inference": "not_established"}
    from .execution_bundle import execution_accounting
    report.update(execution_accounting(bundle))
    write_json(output, report)
    protocol_errors = sum(r["output"]["error"]["code"] == "ARGUMENT_JSON" for r in report["public_protocol_rejections"])
    return {"output": str(output), "tool_calls": len(tools),
            "model_errors": len(errors) + len(schema_rejections) + protocol_errors + len(report["terminal_protocol_failures"]),
            "business_success": effects["task_success"], "compactions": len(report["compactions"])}
