"""Offline input diagnostics; never used to change tools or provide model feedback."""

from ..dataflow.skills import PROCEDURES, KNOWN_SKILLS as SKILLS

KINDS = {"orders_id": "orders", "consent_id": "consent", "rates_id": "rates",
         "joined_id": "joined", "summary_id": "summary", "summary_ids": "summary",
         "rollup_ids": "rollup", "previous_rollup_id": "rollup", "audit_ids": "audit",
         "review_id": "review", "authorized_id": "authorized",
         "reconciliation_id": "reconciliation", "coverage_id": "coverage",
         "allocation_id": "allocation", "fulfillment_ids": "fulfillment", "settlement_id": "settlement",
         "previous_settlement_id": "settlement", "joined_ids": "joined", "credit_assessment_id": "credit_assessment"}


def dataflow_call_diagnostics(runtime, name, arguments):
    args = arguments if isinstance(arguments, dict) else {}
    issues = []
    kinds = dict(KINDS)
    if runtime.project.get("business_program") in {"obligation_sales_v1", "credit_sales_v2"}:
        kinds.update(settlement_id="obligation_settlement", previous_settlement_id="obligation_settlement",
                     settlement_ids="obligation_settlement", collection_ids=("collection", "dispute_resolution"))
    run = runtime.project["active"].get(args.get("run_id")) if isinstance(args.get("run_id"), str) else None
    skill = args.get("skill_id") if name == "begin_skill" else name if name in SKILLS else run["skill_id"] if run else None
    eligible = name in SKILLS or name in {"begin_skill", "revise_skill", "execute_step", "run_skill_script", "finish_skill", "abandon_skill"}
    if not eligible:
        return None
    if name in {"revise_skill", "execute_step", "run_skill_script", "finish_skill", "abandon_skill"} and run is None:
        issues.append({"code": "NO_ACTIVE_RUN", "path": "run_id", "value": args.get("run_id")})

    def visit(value, path, kind=None):
        if isinstance(value, dict):
            for key, item in value.items():
                visit(item, f"{path}.{key}" if path else key, kinds.get(key))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                visit(item, f"{path}[{index}]", kind)
        elif kind and isinstance(value, str) and value:
            artifact = runtime.project["artifacts"].get(value)
            if artifact is None:
                issues.append({"code": "UNKNOWN_ARTIFACT", "path": path, "value": value})
            else:
                if artifact["kind"] not in (kind if isinstance(kind, tuple) else (kind,)):
                    issues.append({"code": "ARTIFACT_KIND", "path": path, "value": value,
                                   "actual": artifact["kind"], "expected": kind})
                if not runtime.current(value):
                    issues.append({"code": "STALE_ARTIFACT", "path": path, "value": value})

    # Script inputs are delegated, not a fresh model binding decision.
    if name not in {"run_skill_script", "finish_skill", "abandon_skill"}:
        visit(args, "")
    if run and name in {"execute_step", "run_skill_script"}:
        if run["output"] is not None:
            issues.append({"code": "ALREADY_COMMITTED", "path": "run_id"})
        elif skill in PROCEDURES:
            position = len(run["steps"])
            requested = args.get("step_id") if name == "execute_step" else "convert" if skill == "join-orders" else None
            expected = PROCEDURES[skill][position][0] if position < len(PROCEDURES[skill]) else None
            if name == "run_skill_script" and skill != "join-orders":
                issues.append({"code": "NO_SCRIPT", "path": "run_id"})
            elif requested != expected:
                issues.append({"code": "STEP_ORDER", "path": "step_id", "expected": expected, "actual": requested})
    return {"skill_id": skill, "issues": issues,
            "explicit_binding_decision": name in SKILLS or name in {"begin_skill", "revise_skill", "execute_step"},
            "interface_selection": name == "begin_skill",
            "scope": "Known artifact type/freshness, run existence and procedural order; not exhaustive business validity or optimal next-skill judgment"}


def summarize_diagnostics(records):
    selections = [r for r in records if r["diagnostic"]["interface_selection"]]
    accepted = [r for r in selections if r["operation_succeeded"]]
    return {"measurement_version": 1, "visibility": "offline_only_never_provider_feedback",
            "records": records, "selection_attempts": len(selections),
            "interface_accepted_selections": len(accepted),
            "accepted_selections_with_detected_binding_issues": sum(bool(r["diagnostic"]["issues"]) for r in accepted),
            "calls_with_multiple_detected_issues": sum(len(r["diagnostic"]["issues"]) > 1 for r in records),
            "note": "Reported tool errors and offline issues are different counts. Repeated bindings are not independent decisions; no first-attempt accuracy is inferred. No detected issue does not establish correctness."}
