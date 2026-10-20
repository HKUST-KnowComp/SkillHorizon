"""Offline recovery accounting by selected business dependencies, not run IDs."""

from copy import deepcopy
import json

from ..dataflow.runtime import ordered
from ..dataflow.skills import KNOWN_SKILLS as SKILLS
from ..errors import DomainError


def maintenance_measurements(project):
    if project.get("domain") != "fieldops":
        return None
    artifacts = project["artifacts"].values()
    repairs = [a for a in artifacts if a["kind"] == "repair"]
    tests = [a for a in artifacts if a["kind"] == "test"]
    return {"basis": "All committed work in this state, including any reference prefix; rejected operations cost no work hours.",
            "clock": "Per-device work within an isolation; parallel devices, not API time or a detailed crew job-shop simulation.",
            "repair_hours": sum(project["devices"][a["data"]["device_id"]]["duration"] for a in repairs),
            "test_hours": len(tests),
            "failed_measurement_hours": sum(a["data"]["passed"] is False for a in tests),
            "rework_repairs": sum(bool(a["data"].get("replaces_repair_id")) for a in repairs),
            "replacement_parts_consumed": sum(a["data"]["method"] == "replace" for a in repairs),
            "isolations": [{"isolation_id": key, "open": value["open"],
                            "window_hours": value["end"] - value["start"],
                            "work_hours_by_device": deepcopy(value["work_hours"])}
                           for key, value in project["isolations"].items()]}


def dependency_scope(project, skill, arguments):
    roots, unresolved = set(), []
    resolved = set()

    def resolve(identifier, visiting):
        if identifier in project["sources"]:
            roots.add(identifier)
        elif identifier in resolved:
            return
        elif identifier in project["artifacts"] and identifier not in visiting:
            for parent in project["artifacts"][identifier]["inputs"]:
                resolve(parent["id"], visiting | {identifier})
            resolved.add(identifier)
        else:
            unresolved.append(identifier)

    for field, value in arguments.items():
        if field == "invoice_ids":
            continue  # Business keys are resolved through the supplied settlement artifacts.
        if field.endswith("_id") or field.endswith("_ids"):
            for identifier in value if isinstance(value, list) else [value]:
                if isinstance(identifier, str) and identifier:
                    resolve(identifier, set())
                elif identifier != "":
                    unresolved.append(repr(identifier))
    if not roots or unresolved:
        return None
    return {"skill_id": skill,
            **({"invoice_ids": sorted(arguments["invoice_ids"]), "period": arguments.get("period")}
               if isinstance(arguments.get("invoice_ids"), list) else {}), "sources": [
        {"id": key, "revision": project["sources"][key]["revision"]} for key in sorted(roots)]}


def _content_check(runtime, artifact):
    kind, region, period = artifact["kind"], artifact.get("region"), artifact.get("period")
    actual = ordered(runtime._rows(artifact))
    if runtime.project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"} and kind in {
            "allocation", "fulfillment", "obligation_settlement", "collection", "dispute_resolution", "credit_assessment"}:
        from ..dataflow.obligation_verification import check_artifact
        return check_artifact(runtime, artifact)
    if kind in {"orders", "joined", "summary"}:
        orders, joined, summary = runtime.expected_region(region, period)
        expected = {"orders": orders, "joined": joined, "summary": [summary]}[kind]
    elif kind in {"consent", "rates"}:
        source = runtime.project["sources"][artifact["inputs"][0]["id"]]
        rows = runtime._rows(source)
        expected = ([{k: r[k] for k in ("customer_id", "region")} for r in rows
                     if r["analytics_allowed"] == "yes"] if kind == "consent" else rows)
    elif kind in {"rollup", "publication"}:
        expected = [r for r in runtime.expected_release() if period is None or r["period"] == period]
    elif kind == "audit":
        return runtime._audit_valid(artifact)
    elif kind == "review":
        runtime._review_inputs(artifact["arguments"])
        expected = [{"passed": 1}]
    elif kind in {"allocation", "fulfillment", "settlement"}:
        from ..dataflow.verification import funding_content_from_public_sources
        expected = funding_content_from_public_sources(runtime.root, runtime.project)[kind, region, period]
        if kind == "fulfillment":
            _, joined, _ = runtime.expected_region(region, period)
            by_id = {row["order_id"]: row for row in joined}
            expected = [{**by_id[row["order_id"]], **row} for row in expected]
    else:
        return None
    return actual == ordered(expected)


class RecoveryMeasurements:
    def __init__(self, prefix_actions=0):
        self.prefix_actions = prefix_actions
        self.groups = {}
        self.unresolved = []
        self.tool_index = 0

    def observe(self, before, runtime, name, arguments, output, turn, diagnostic=None):
        self.tool_index += 1
        args = arguments if isinstance(arguments, dict) else {}
        run_id = args.get("run_id")
        run = before["active"].get(run_id) if isinstance(run_id, str) else None
        if name in {"begin_skill", "revise_skill"}:
            skill = args.get("skill_id") if name == "begin_skill" else run["skill_id"] if run else None
            selected = args.get("arguments")
        elif name in {"execute_step", "run_skill_script"} and run:
            skill, selected = run["skill_id"], run["arguments"]
        elif name in SKILLS:
            skill, selected = name, args
        else:
            return
        if not isinstance(skill, str) or skill not in SKILLS or not isinstance(selected, dict):
            self.unresolved.append({"turn": turn, "tool": name, "reason": "unknown_skill_or_selection"})
            return
        scope = dependency_scope(before, skill, selected)
        if scope is None:
            self.unresolved.append({"turn": turn, "tool": name, "skill_id": skill,
                                    "reason": "unresolved_selected_dependencies"})
            return
        key = json.dumps(scope, sort_keys=True)
        group = self.groups.setdefault(key, {"scope": scope, "attempts": [], "episodes": []})
        issues = (diagnostic or {}).get("issues", [])
        row = {"turn": turn, "tool_index": self.tool_index, "tool": name, "run_id": run_id,
               "operation_succeeded": output.get("ok") is True,
               "binding_issue_codes": sorted({i["code"] for i in issues}),
               "error_code": output.get("error", {}).get("code"), "commits": []}
        binding = name in {"begin_skill", "revise_skill"} or name in SKILLS
        if binding and "first_evaluated_binding" not in group:
            group["first_evaluated_binding"] = {"turn": turn, "tool_index": self.tool_index, "known_issues": bool(issues),
                                                "operation_succeeded": row["operation_succeeded"]}
        committed = runtime.project["artifacts"].keys() - before["artifacts"].keys()
        # Evaluate against pre-action sources: an event fired by this very commit
        # must not retroactively turn a correct computation into a model error.
        after = runtime.project
        evaluation = deepcopy(after)
        evaluation["sources"] = deepcopy(before["sources"])
        try:
            runtime.project = evaluation
            for identifier in sorted(committed):
                artifact = evaluation["artifacts"][identifier]
                if artifact.get("intermediate"):
                    continue
                try:
                    content = _content_check(runtime, artifact)
                    current = runtime.current(identifier)
                except (DomainError, ValueError, KeyError):
                    content, current = None, runtime.current(identifier)
                equivalents = [key for key, prior in before["artifacts"].items()
                    if content is True and current and not prior.get("intermediate")
                    and (prior["kind"], prior.get("region"), prior.get("period")) ==
                        (artifact["kind"], artifact.get("region"), artifact.get("period"))
                    and runtime.current(key) and ordered(runtime._rows(prior)) == ordered(runtime._rows(artifact))]
                commit = {"artifact_id": identifier, "kind": artifact["kind"],
                          "content_correct_before_event": content, "current_before_event": current,
                          "reusable_prior_artifact_ids": sorted(equivalents)}
                row["commits"].append(commit)
                group.setdefault("first_evaluated_commit", {"turn": turn, "tool_index": self.tool_index, **deepcopy(commit)})
        finally:
            runtime.project = after
        for commit in row["commits"]:
            commit["current_after_event"] = runtime.current(commit["artifact_id"])
        group["attempts"].append(row)
        failure = not row["operation_succeeded"] or bool(issues) or any(
            c["content_correct_before_event"] is False or not c["current_before_event"] for c in row["commits"])
        pending = next((e for e in reversed(group["episodes"]) if e["status"] == "unresolved"), None)
        if failure:
            if pending is None:
                pending = {"first_failure_turn": turn, "first_failure_tool_index": self.tool_index,
                           "failed_attempts": 0, "status": "unresolved"}
                group["episodes"].append(pending)
            pending["failed_attempts"] += 1
        elif pending and any(c["content_correct_before_event"] is True and c["current_before_event"] for c in row["commits"]):
            pending.update(status="recovered", recovery_turn=turn,
                           intervening_tool_calls=self.tool_index - pending["first_failure_tool_index"])

    def report(self, project):
        groups = deepcopy(list(self.groups.values()))
        for group in groups:
            if any(project["sources"][s["id"]]["revision"] != s["revision"] for s in group["scope"]["sources"]):
                for episode in group["episodes"]:
                    if episode["status"] == "unresolved":
                        episode["status"] = "superseded_source_epoch"
        episodes = [e for g in groups for e in g["episodes"]]
        commits = [c for g in groups for a in g["attempts"] for c in a["commits"]]
        return {"measurement_version": 1, "visibility": "offline_only_never_provider_feedback",
                "domain": "dataops", "evaluation_prefix_actions": self.prefix_actions,
                "scope_note": "First evaluated attempt, not lifetime first after a checkpoint. Groups use selected transitive source IDs and current revisions, not run IDs. Unknown dependencies remain ungrouped; a correction to a different source set is not linked automatically.",
                "content_check_basis": "Public-source business checks; only final publication has a separately implemented independent verifier.",
                "recovery_note": "Known binding problems, rejected operations and incorrect commits are retained separately in attempts. Recovery ends at a correct full commit in the same dependency epoch; finish/abandon do not reset history. Unresolved means no such later commit, not necessarily an unmet business goal. Delay counts tool calls, excluding provider-only retries.",
                "reuse_note": "Equivalent current content existed before this commit; not proof of a globally optimal alternative plan.",
                "groups": groups, "unresolved_scope_attempts": self.unresolved,
                "group_count": len(groups), "recovered_episodes": sum(e["status"] == "recovered" for e in episodes),
                "unresolved_episodes": sum(e["status"] == "unresolved" for e in episodes),
                "superseded_episodes": sum(e["status"] == "superseded_source_epoch" for e in episodes),
                "commits_with_reusable_prior_content": sum(bool(c["reusable_prior_artifact_ids"]) for c in commits)}
