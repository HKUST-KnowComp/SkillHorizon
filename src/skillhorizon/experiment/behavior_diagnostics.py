"""Offline diagnostic overlay; never changes scoring, prompts or historical results."""

from collections import Counter
from pathlib import Path
import shutil
import tempfile

from ..util import load_json, load_jsonl, write_json
from .messages import parse_response
from .scoring import score_run


def maintenance_decisions(checkpoint, final, records):
    baseline, world = checkpoint["maintenance"], final["maintenance"]
    if baseline["evidence"] != world["evidence"] or baseline["policies"] != world["policies"]:
        raise ValueError("Decision diagnosis requires this profile's immutable evidence and policies")
    decision_turns, commit_turns = {}, {}
    for record in records:
        if not record["executed"] or record["output"].get("ok") is not True:
            continue
        args, out = record["arguments"], record["output"]
        if record["name"] == "execute_step" and args.get("step_id") == "decide" and "decision_id" in out:
            decision_turns[out["decision_id"]] = record["turn"]
        if record["name"] == "execute_step" and args.get("step_id") == "commit" and "plan_id" in out:
            commit_turns[out["plan_id"]] = record["turn"]

    def assess(identifier):
        decision = world["decisions"][identifier]
        change = baseline["changes"][decision["change_id"]]
        proof = baseline["evidence"][change["evidence_id"]]
        assessment = world["assessments"][decision["assessment_id"]]
        policy = baseline["policies"][assessment["policy_id"]]
        expected = "apply" if proof["status"] == "confirmed" else "reject"
        delta = proof["settled_amount_cents"] - proof["booked_amount_cents"]
        issues = []
        if decision["choice"] != expected:
            issues.append("disposition")
        if expected == "apply" and assessment["delta_cents"] != delta:
            issues.append("adjustment_amount")
        if change["region"] not in policy["regions"] or change["period"] not in policy["periods"]:
            issues.append("policy_scope")
        if decision["choice"] == "apply" and abs(assessment["delta_cents"]) > policy["max_adjustment_cents"]:
            issues.append("policy_limit")
        return {"decision_id": identifier, "turn": decision_turns.get(identifier),
                "origin": "reference_prefix" if identifier in baseline["decisions"] else "evaluated_policy",
                "choice": decision["choice"], "expected_choice": expected, "proposed_delta_cents": assessment["delta_cents"],
                "expected_delta_cents": delta, "correct": not issues, "issue_dimensions": issues}

    rows = []
    for identifier, change in baseline["changes"].items():
        attempts = [assess(key) for key in decision_turns if world["decisions"][key]["change_id"] == identifier]
        attempts.sort(key=lambda row: row["turn"])
        resolution = world["changes"][identifier]["resolution"]
        committed = None
        if resolution:
            committed = {**assess(resolution["decision_id"]), "commit_turn": commit_turns.get(resolution["plan_id"]),
                         "commit_origin": "reference_prefix" if change["resolution"] else "evaluated_policy",
                         "correction_available_after_commit": False}
        rows.append({"change_id": identifier, "evaluated_decisions": attempts,
                     "first_evaluated_decision": attempts[0] if attempts else None,
                     "final_committed_decision": committed,
                     "corrected_before_commit": bool(attempts and not attempts[0]["correct"] and committed and committed["correct"]),
                     "irreversible_wrong_commit": bool(committed and not committed["correct"])})
    first = [row["first_evaluated_decision"] for row in rows if row["first_evaluated_decision"]]
    committed = [row["final_committed_decision"] for row in rows if row["final_committed_decision"]]
    return {"rows": rows, "first_evaluated_decisions": len(first), "first_evaluated_correct": sum(x["correct"] for x in first),
            "committed_changes": len(committed), "committed_correct": sum(x["correct"] for x in committed),
            "evaluated_irreversible_wrong_commits": sum(not x["correct"] and x["commit_origin"] == "evaluated_policy" for x in committed),
            "inherited_irreversible_wrong_commits": sum(not x["correct"] and x["commit_origin"] == "reference_prefix" for x in committed),
            "corrected_before_commit": sum(row["corrected_before_commit"] for row in rows),
            "post_commit_recovery_denominator": 0,
            "note": "Committed changes are immutable in release_maintenance_v2; no compensation action exists. Wrong commits are not missed post-commit recovery opportunities. Prose is not scored. Prefix decisions are excluded from first evaluated decisions."}


def diagnostic_summary(task, result, checkpoint, final, records):
    if not result.get("valid"):
        raise ValueError("Cannot attribute behavior from invalid execution evidence")
    errors = [{"turn": row["turn"], "tool": row["name"], "error_code": row["output"]["error"]["code"]}
              for row in records if row["output"].get("ok") is False]
    skills = result["skill_execution"]
    recovery = result.get("business_recovery", {})
    selections = result.get("offline_call_diagnostics", {})
    failed_runs = {row["run_id"] for row in skills["calls"] if not row["operation_succeeded"] and row["run_id"]}
    completed = [row for row in skills["runs"] if row["origin"] == "evaluated_policy" and row["status"] == "completed"]
    decision = maintenance_decisions(checkpoint, final, records) if final.get("workspace_profile") == "release_maintenance_v2" else None
    return {"diagnostic_revision": "skill_behavior_v1", "visibility": "offline_only_never_provider_feedback",
            "evidence_source": result["evidence_source"], "evaluation_track": result["evaluation_track"],
            "task_success": result["task_success"], "outcome": result["outcome"],
            "selection": {"attempts": selections.get("selection_attempts"),
                          "interface_accepted": selections.get("interface_accepted_selections"),
                          "accepted_with_known_binding_issues": selections.get("accepted_selections_with_detected_binding_issues"),
                          "optimal_next_skill_accuracy": None,
                          "note": "Interface admissibility and known input issues only; legal alternative schedules are not compared with expert next actions."},
            "execution": {"tool_error_responses": errors, "error_counts": dict(Counter(row["error_code"] for row in errors)),
                          "completed_runs": len(completed),
                          "completed_runs_without_reported_call_error": sum(row["run_id"] not in failed_runs for row in completed),
                          "script_calls": sum(row["script_calls"] for row in skills["per_skill"].values()),
                          "note": "No reported tool error does not establish business correctness."},
            "dependency_recovery": {key: recovery.get(key) for key in (
                "scope_note", "recovery_note", "groups", "recovered_episodes", "unresolved_episodes", "superseded_episodes", "unresolved_scope_attempts")},
            "efficiency": {"commits_with_reusable_prior_content": recovery.get("commits_with_reusable_prior_content"),
                           "note": "Equivalent current content is a candidate for reuse, not proof that every recomputation is unnecessary."},
            "lifecycle": {"complete": result["lifecycle_complete"], "open_runs": skills["open_runs_at_stop"],
                          "abandoned_runs": sum(row["abandoned_runs"] for row in skills["per_skill"].values()),
                          "note": "Abandonment retains committed effects. Unrelated open runs are not automatic business failures."},
            "business_decisions": decision,
            "business_verification": {"publication": result.get("independent_content_verification"),
                                      "maintenance": result.get("independent_maintenance_verification")},
            "infrastructure": result["budget_consumed"], "litm_inference": "not_established"}


def diagnose_run(root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(root):
        raise ValueError("Write a new diagnostic file outside the immutable run directory")
    recorded = load_json(root / "RESULT.json")
    with tempfile.TemporaryDirectory(prefix="skillhorizon-diagnosis-") as tmp:
        copied = Path(tmp) / "run"
        shutil.copytree(root, copied)
        if score_run(copied) != recorded:
            raise ValueError("Recorded result differs from independent replay; use its archived runtime")
    task, run = load_json(root / "task.json"), load_json(root / "run.json")
    records = []
    for event in load_jsonl(root / "events.jsonl"):
        if not event["response_accepted"]:
            continue
        calls, _ = parse_response(load_json(root / "turns" / f'{event["turn"]:04d}' / "response.json"), run["config"]["model"])
        for call, effect in zip(calls, event["results"]):
            records.append({"turn": event["turn"], "name": call["name"], "arguments": call["arguments"],
                            "output": effect["output"], "executed": effect["executed"]})
    report = diagnostic_summary(task, recorded, load_json(root / "checkpoint/project.json"),
                                load_json(root / "runtime/project.json"), records)
    report["source_run"] = str(root)
    report["independent_replay_matches_original"] = True
    report["resumption"] = recorded.get("resumption")
    write_json(output, report)
    return report
