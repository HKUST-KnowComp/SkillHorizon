"""Independent row-wise maintenance checks; never returned by a public tool."""

from decimal import Decimal

from ..util import load_json
from .runtime import ordered, read_table_file


def verify_maintenance(root, baseline_root=None):
    from pathlib import Path
    root = Path(root)
    baseline_root = Path(baseline_root) if baseline_root else root
    project = load_json(root / "project.json")
    baseline = load_json(baseline_root / "project.json")["maintenance"]
    world = project["maintenance"]
    errors = []
    expected = {key: read_table_file(baseline_root / history[0]["path"])
                for key, history in baseline["source_history"].items()}
    component_amounts = {key: value["booked_amount_cents"] for key, value in baseline["components"].items()}
    for identifier, change in baseline["changes"].items():
        proof = baseline["evidence"][change["evidence_id"]]
        applicable = proof["status"] == "confirmed"
        delta = proof["settled_amount_cents"] - proof["booked_amount_cents"]
        resolution = world["changes"].get(identifier, {}).get("resolution")
        if not resolution or resolution["choice"] != ("apply" if applicable else "reject"):
            errors.append(f"Unresolved or inappropriate disposition: {identifier}")
        if resolution:
            decision = world["decisions"].get(resolution["decision_id"], {})
            plan = world["plans"].get(resolution["plan_id"], {})
            assessment = world["assessments"].get(decision.get("assessment_id"), {})
            if decision.get("change_id") != identifier or not plan.get("committed") or plan.get("decision_id") != resolution["decision_id"]:
                errors.append(f"Resolution lacks a committed matching decision: {identifier}")
            if (decision.get("superseded_by") is not None or decision.get("choice") != resolution["choice"]
                    or plan.get("choice") != resolution["choice"] or assessment.get("change_id") != identifier
                    or assessment.get("evidence_id") != change["evidence_id"]):
                errors.append(f"Resolution decision lineage is inconsistent: {identifier}")
            if applicable and (assessment.get("delta_cents") != delta or plan.get("delta_cents") != delta):
                errors.append(f"Per-component adjustment differs from its settlement evidence: {identifier}")
        if applicable:
            component_amounts[proof["component_id"]] += delta
            for row in expected[change["source_id"]]:
                if row["order_id"].strip() == change["order_id"]:
                    row["amount"] = format(Decimal(row["amount"]) + Decimal(delta) / 100, ".2f")
    for identifier, amount in component_amounts.items():
        if world["components"].get(identifier, {}).get("amount_cents") != amount:
            errors.append(f"Ledger component content differs from evidence: {identifier}")
    for identifier, rows in expected.items():
        actual = read_table_file(root / project["sources"][identifier]["path"])
        if ordered(rows) != ordered(actual):
            errors.append(f"Source content differs from evidence or non-target rows changed: {identifier}")
    for path, rows in baseline["retained_tables"].items():
        if not (root / path).is_file() or ordered(read_table_file(root / path)) != ordered(rows):
            errors.append(f"Historical consumer content changed: {path}")
    publication = project["artifacts"].get(project["publication"], {})
    review_id = publication.get("arguments", {}).get("review_id")
    review = project["artifacts"].get(review_id, {})
    used = review.get("arguments", {}).get("audit_ids", [])
    for identifier, consumer in baseline["consumers"].items():
        if world["consumers"].get(identifier) != consumer or consumer["audit_id"] not in used:
            errors.append(f"Unchanged consumer audit was not retained and reused: {identifier}")
    return {"passed": not errors, "errors": errors,
            "checks": ["evidence_based_disposition", "per_component_settlement_amount", "all_source_rows_including_non_targets", "committed_decision_binding",
                       "historical_snapshot_immutability", "unaffected_consumer_audit_reuse"],
            "scope": "offline_business_checks_not_language_rationale_scoring"}
