"""Offline SDK process measurements over independently replayed business effects."""

from collections import Counter
from copy import deepcopy

from .binding_diagnostics import dataflow_call_diagnostics, summarize_diagnostics
from .recovery_metrics import RecoveryMeasurements, _content_check
from .skill_metrics import skill_execution_measurements


def completion_state(final, effects, skills, stop_reason):
    """Offline completion only for runtimes with the public project/active contract."""
    from ..public_contract import lifecycle

    if effects.get("valid") is not True:
        raise ValueError("Completion state requires verified effects")
    state = effects.get("state")
    if state is not None and not isinstance(state, dict):
        raise ValueError("Completion state must be a mapping")

    def boolean(key):
        value = state.get(key) if state is not None else None
        if value is not None and type(value) is not bool:
            raise ValueError("Completion state flag must be boolean or unknown")
        return value

    def conjunction(*values):
        if any(value is False for value in values):
            return False
        return None if any(value is None for value in values) else True

    business = boolean("success")
    handoff = boolean("termination_ready")
    closed = boolean("lifecycle_complete")
    project = final
    if project is not None and not isinstance(project, dict):
        raise ValueError("Completion project must be a mapping")
    active = project.get("active") if project is not None else None
    if active is not None and not isinstance(active, dict):
        raise ValueError("Completion active runs must be a mapping")
    open_runs = len(active) if active is not None else None
    measured = skills.get("open_runs_at_stop")
    if measured is not None and (type(measured) is not int or measured < 0):
        raise ValueError("Completion skill open count must be a nonnegative integer")
    conserved = None
    if open_runs is not None and measured is not None:
        if open_runs != measured:
            raise ValueError("Completion open run counts disagree")
        conserved = True
    if open_runs is not None and closed is not None and closed != (open_runs == 0):
        raise ValueError("Completion closed flag disagrees with active runs")

    terminal = nonterminal = None
    if active is not None:
        outputs_known = True
        for key, run in active.items():
            if not isinstance(key, str) or not isinstance(run, dict):
                raise ValueError("Completion active run shape is invalid")
            if "output" not in run:
                outputs_known = False
                continue
            output = run["output"]
            if output is not None and not isinstance(output, dict):
                raise ValueError("Completion run output must be a mapping or null")
            if output is not None and not isinstance(output.get("artifact_id"), str):
                raise ValueError("Completion run output artifact identity is invalid")
        deliverable_known = "publication" in project or "acceptance" in project
        deliverable = project.get("publication", project.get("acceptance"))
        if deliverable is not None and not isinstance(deliverable, str):
            raise ValueError("Completion deliverable identity is invalid")
        if outputs_known and deliverable_known:
            terminal = len(lifecycle(active, deliverable)["terminal_run_ids"])
            nonterminal = open_runs - terminal
    if business is not None and handoff is not None and terminal is not None:
        if handoff != (business and terminal == 0):
            raise ValueError("Completion handoff flag disagrees with business and terminal runs")
    return {"business_success": business, "handoff_ready": handoff, "all_runs_closed": closed,
            "open_runs": open_runs, "terminal_open_runs": terminal, "nonterminal_open_runs": nonterminal,
            "open_run_counts_conserved": conserved,
            "final_with_handoff_pending": conjunction(
                None if stop_reason is None else stop_reason == "sdk_final_output",
                business, None if handoff is None else not handoff),
            "optional_housekeeping_pending": conjunction(
                business, handoff, None if nonterminal is None else nonterminal > 0)}


def request_accounting(attempts):
    rejected = [row for row in attempts if not row["accepted"]]
    return {"requests": len(attempts),
            "usable_actor_responses": sum(r["accepted"] and r["role"] == "actor" for r in attempts),
            "accepted_summaries": sum(r["accepted"] and r["role"] == "compaction" for r in attempts),
            "rejected_requests": len(rejected),
            "known_tokens": sum(r["reported_tokens"] or 0 for r in attempts),
            "rejected_known_tokens": sum(r["reported_tokens"] or 0 for r in rejected),
            "unknown_usage_requests": sum(r["reported_tokens"] is None for r in attempts),
            "rejected_statuses": dict(Counter(r["status"] for r in rejected)),
            "scope": "Provider-reported consumption; unknown is additional, not zero. Not currency or distinct context size."}


class CompletionMeasurements:
    """Offline observed milestones; later regressions are retained, not erased."""

    def __init__(self):
        self.transitions = []
        self.state = None
        self.business_success = False
        self.case_progress = []
        self.completed_cases = set()

    def observe(self, runtime, name, turn, tool_index=None, *, business_index=None):
        read_only = {"read_project", "read_table", "read_skill_file", "search_objects", "read_object",
                     "object_history", "read_source_version", "finish_skill", "abandon_skill"}
        if self.state is None or name not in read_only:
            score = runtime.score()
            self.business_success = bool(score["success"])
            cases = score.get("cases", {})
            completed = {key for key, value in cases.items() if value["success"]}
            if not self.case_progress or completed != self.completed_cases:
                self.case_progress.append({"turn": turn, "tool_index": tool_index,
                    "completed": len(completed), "total": len(cases),
                    "rate": len(completed)/len(cases) if cases else None,
                    "gained": sorted(completed-self.completed_cases),
                    "lost": sorted(self.completed_cases-completed)})
                self.completed_cases = completed
        from ..public_contract import completion
        state = completion(self.business_success, runtime.project["active"], runtime.project.get("publication", runtime.project.get("acceptance")))
        observed = {"business_success": state["success"], "handoff_ready": state["termination_ready"],
                    "all_runs_closed_after_success": state["success"] and state["lifecycle_complete"]}
        if observed != self.state:
            self.transitions.append({"turn": turn, "tool_index": tool_index,
                                     "business_index": tool_index if business_index is None else business_index,
                                     **observed})
            self.state = observed

    def report(self, attempts, records, stop_reason):
        first = {key: next((r["turn"] for r in self.transitions if r[key]), None) for key in
                 ("business_success", "handoff_ready", "all_runs_closed_after_success")}
        business, handoff = first["business_success"], first["handoff_ready"]
        first_tools = {key: next((r["tool_index"] for r in self.transitions if r[key]), None) for key in first}
        first_business = {key: next((r["business_index"] for r in self.transitions if r[key]), None) for key in first}
        phases = {}
        if business is not None:
            phases["through_first_business_success"] = request_accounting([r for r in attempts if r["turn"] <= business])
        if handoff is not None:
            phases["after_business_through_first_handoff"] = request_accounting(
                [r for r in attempts if business < r["turn"] <= handoff])
            phases["after_first_handoff"] = request_accounting([r for r in attempts if r["turn"] > handoff])
        return {"first_observed_turns": first, "first_observed_tool_indices": first_tools,
                "first_observed_business_indices": first_business,
                "state_transitions": self.transitions,
                "case_progress": {"transitions": self.case_progress,
                    "initial": self.case_progress[0] if self.case_progress else None,
                    "final": self.case_progress[-1] if self.case_progress else None,
                    "maximum_completed": max((r["completed"] for r in self.case_progress), default=0),
                    "regressed_cases": sum(len(r["lost"]) for r in self.case_progress),
                    "scope": "Offline domain-verifier cases, not work fraction or minimal-plan progress. Current progress can decrease. Case completion alone is not final handoff success."},
                "phase_costs": phases, "natural_final_response": stop_reason == "sdk_final_output",
                "post_handoff_tool_counts": dict(Counter(r["name"] for i, r in enumerate(records, 1)
                    if first_business["handoff_ready"] is not None
                    and r.get("business_index", r.get("tool_index", i)) > first_business["handoff_ready"])),
                "tool_role_counts": {"run_management": sum(r["name"] in {"begin_skill", "finish_skill", "abandon_skill", "revise_skill"} for r in records),
                                     "step_or_script": sum(r["name"] in {"execute_step", "run_skill_script"} for r in records)},
                "scope": "Offline state after evaluated tool actions, not checkpoint achievement time. First success may regress. After-handoff work is not necessarily cleanup. No model feedback or automatic stopping."}


class NativeProcessMeasurements:
    def __init__(self, task, initial, records, attempts):
        self.task, self.initial, self.attempts = task, initial, attempts
        self.records = [{**row, "business_index": index} for index, row in enumerate(records, 1)]
        self.dataflow = task["format"] == "skillhorizon.dataflow.v2"
        self.recovery = RecoveryMeasurements(len(initial)) if self.dataflow else None
        self.bindings, self.waves = [], []
        self.actions = list(initial)
        self.completion = CompletionMeasurements()
        from ..fieldops.metrics import OperationsProcessMeasurements
        self.operations = OperationsProcessMeasurements() if not self.dataflow else None

    def observe(self, before, runtime, action, index):
        record = self.records[index - len(self.initial)]
        name, args, output, turn = record["name"], record["arguments"], record["output"], record["turn"]
        if (name, args, output) != (action["skill_id"], action["arguments"], action["result"]):
            raise ValueError("Native process measurement differs from replayed tool")
        self.actions.append(action)
        self.completion.observe(runtime, name, turn, record.get("tool_index", index - len(self.initial) + 1),
                                business_index=record["business_index"])
        if not self.dataflow:
            self.operations.observe(before, runtime, record)
            return
        after = runtime.project
        # Diagnostics refer to what was true before dispatch, including a commit
        # that triggers an external source update after computing a valid output.
        try:
            runtime.project = before
            diagnostic = dataflow_call_diagnostics(runtime, name, args)
            prior_current = {key for key in before["artifacts"] if runtime.current(key)}
        finally:
            runtime.project = after
        self.recovery.observe(before, runtime, name, args, output, turn, diagnostic)
        if diagnostic is not None:
            self.bindings.append({"turn": turn, "tool": name, "diagnostic": diagnostic,
                                  "operation_succeeded": output.get("ok") is True,
                                  "returned_error": output.get("error", {}).get("code")})
        committed = after["artifacts"].keys() - before["artifacts"].keys()
        for key in committed:
            artifact = after["artifacts"][key]
            if not artifact.get("intermediate") and artifact["kind"] in {"review", "publication"}:
                evaluation = deepcopy(after)
                evaluation["sources"] = deepcopy(before["sources"])
                try:
                    runtime.project = evaluation
                    valid_frontier = runtime.current(key) and _content_check(runtime, artifact) is True
                finally:
                    runtime.project = after
                if valid_frontier:
                    for wave in self.waves:
                        if wave["status"] == "unresolved":
                            wave.update(status="reviewed" if artifact["kind"] == "review" else "published",
                                        frontier_turn=turn, frontier_artifact=key,
                                        tool_calls_to_frontier=index - wave["action_index"])
            parents = {p["id"] for p in artifact["inputs"] if p["type"] == "artifact"}
            for wave in self.waves:
                reused = parents & set(wave["unaffected_artifact_ids"]) & prior_current
                wave["reused_unaffected_artifact_ids"] = sorted(set(wave["reused_unaffected_artifact_ids"]) | reused)
        changes = [{"source_id": key, "before_revision": source["revision"],
                    "after_revision": after["sources"][key]["revision"]}
                   for key, source in before["sources"].items()
                   if source["revision"] != after["sources"][key]["revision"]]
        if changes:
            for wave in self.waves:
                if wave["status"] == "unresolved":
                    wave.update(status="superseded_by_update", superseded_turn=turn)
            evaluation = deepcopy(after)
            evaluation["sources"] = deepcopy(before["sources"])
            try:
                runtime.project = evaluation
                before_event = {key for key in evaluation["artifacts"] if runtime.current(key)}
            finally:
                runtime.project = after
            invalidated = {key for key in before_event if not runtime.current(key)}
            self.waves.append({"turn": turn, "action_index": index, "source_changes": changes,
                "status": "unresolved", "invalidated_artifact_ids": sorted(invalidated),
                "invalidated_kinds": dict(Counter(after["artifacts"][key]["kind"] for key in invalidated)),
                "unaffected_artifact_ids": sorted(prior_current - invalidated),
                "reused_unaffected_artifact_ids": []})

    def report(self, final, effects, stop_reason):
        records = [{**row, "executed": True} for row in self.records]
        skills = skill_execution_measurements(self.task, self.actions, records)
        completed = completion_state(final, effects, skills, stop_reason)
        recovery = self.recovery.report(final) if self.recovery else None
        first = []
        if recovery:
            for group in recovery["groups"]:
                commit = group.get("first_evaluated_commit")
                preceding = [row for row in group["attempts"] if commit and row["tool_index"] <= commit["tool_index"]]
                errors = sum(not row["operation_succeeded"] or bool(row["binding_issue_codes"]) for row in preceding)
                known = commit is not None and commit["content_correct_before_event"] is not None
                correct = bool(known and commit["content_correct_before_event"] and commit["current_before_event"])
                first.append({"scope": group["scope"], "first_binding": group.get("first_evaluated_binding"),
                    "first_full_commit": commit, "known_errors_through_first_commit": errors,
                    "first_commit_correct": correct if known else None,
                    "first_pass_without_known_error": correct and not errors if known else None})
                for episode in group["episodes"]:
                    end = episode.get("recovery_turn", max((r["turn"] for r in self.attempts), default=0))
                    span = [r for r in self.attempts if episode["first_failure_turn"] <= r["turn"] <= end]
                    episode["observed_request_interval"] = request_accounting(span)
                    episode["interval_end"] = "recovery" if "recovery_turn" in episode else "observation_stop"
        known = [row for row in first if row["first_commit_correct"] is not None]
        operations = self.operations.report() if self.operations else None
        if operations:
            for event in operations["source_event_recoveries"]:
                if event["recovery_measurement_supported"]:
                    end = event["recovery_turn"] or max((r["turn"] for r in self.attempts), default=0)
                    event["observed_request_interval"] = request_accounting(
                        [r for r in self.attempts if event["event_turn"] <= r["turn"] <= end])
                    event["interval_scope"] = "Event through recovered frontier or observation stop; includes normal work and infra, not minimal avoidable recovery cost"
        return {"revision": "native_business_process_v1", "visibility": "offline_only_never_provider_feedback",
            "final_business_success": effects["task_success"],
            "completion_state": completed,
            "completion_milestones": self.completion.report(self.attempts, self.records, stop_reason),
            "operations_process": operations,
            "termination": {"stop_reason": stop_reason,
                "premature_final_output": stop_reason == "sdk_final_output" and not effects["task_success"],
                "actor_budget_exhausted": stop_reason == "turn_budget",
                "tool_budget_exhausted": stop_reason == "tool_budget",
                "note": "Final text with unfinished business is distinct from infrastructure, capacity and budget stops."},
            "first_execution": {"groups": first, "groups_with_known_first_commit": len(known),
                "correct_first_commits": sum(row["first_commit_correct"] for row in known),
                "first_pass_without_known_error": sum(row["first_pass_without_known_error"] for row in known),
                "groups_without_known_first_commit": len(first) - len(known),
                "unresolved_binding_attempts": len(recovery["unresolved_scope_attempts"]) if recovery else None,
                "scope": "Selected source sets and revisions, not new run IDs. Missing bindings remain ungrouped. Only final publication has an independent content implementation; no optimal-next-skill accuracy."} if self.dataflow else None,
            "skill_execution": skills, "binding_diagnostics": summarize_diagnostics(self.bindings) if self.dataflow else None,
            "business_recovery": recovery,
            "source_updates": {"waves": self.waves,
                "scope": "Source revisions, including exogenous events and model commits. Review recovery is checked before an event fired by that review; it is not final publication success."},
            "lifecycle": {"complete": completed["all_runs_closed"],
                "open_runs": skills["open_runs_at_stop"],
                "evaluated_completed_runs": sum(r["status"] == "completed" and r["origin"] == "evaluated_policy" for r in skills["runs"]),
                "evaluated_abandoned_runs": sum(r["status"] == "abandoned" and r["origin"] == "evaluated_policy" for r in skills["runs"])},
            "infrastructure": request_accounting(self.attempts),
            "recovery_cost_note": "Intervals include the first failing request through recovery or observation stop; they may overlap. Never sum them as avoidable cost. Unknown/different dependency scopes are not linked speculatively."}
