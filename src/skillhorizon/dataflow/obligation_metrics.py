"""Offline effective-dependency diagnostics, separate from model ability."""

from collections import Counter

def obligation_metrics(runtime):
    current = [a for a in runtime.project["artifacts"].values() if runtime.current(a["artifact_id"])]
    books = [a for a in current if a["kind"] == "obligation_settlement"]
    invoices = {row["invoice_id"]: row for book in books for row in runtime._rows(book) if row["record_type"] == "invoice"}
    receipts = [a for a in current if a["kind"] in {"collection", "dispute_resolution"}]
    applied = {identifier for a in current if a["kind"] == "allocation" for identifier in a["arguments"]["collection_ids"]}
    credits = {(row["period"], row["invoice_id"]): row for item in receipts if item["artifact_id"] in applied for row in runtime._rows(item)}
    outstanding = [row for key, row in invoices.items() if (row["due_period"], key) not in credits]
    applied_receipts = [a for a in receipts if a["artifact_id"] in applied]
    joined_origins = [len({row["origin_period"] for row in runtime._rows(item)}) for item in applied_receipts]
    receipt_rows = {a["artifact_id"]: runtime._rows(a) for a in applied_receipts}
    allocation_origins = [len({row["origin_period"] for identifier in a["arguments"]["collection_ids"]
                               for row in receipt_rows.get(identifier, [])})
                          for a in current if a["kind"] == "allocation"]
    ages = Counter(int(row["period"][1:]) - int(row["origin_period"][1:]) for row in credits.values())
    positive_ages = Counter(int(row["period"][1:]) - int(row["origin_period"][1:])
                            for row in credits.values() if row["amount_cents"] > 0)
    positive_origins = [len({row["origin_period"] for identifier in a["arguments"]["collection_ids"]
                            for row in receipt_rows.get(identifier, []) if row["amount_cents"] > 0})
                        for a in current if a["kind"] == "allocation"]
    horizon = runtime.project["periods"][-1]
    constrained = {(a["region"], a["period"]) for a in current if a["kind"] == "fulfillment"
                   and any(not row["fulfilled"] for row in runtime._rows(a))}
    return {"profile": "persistent_invoice_obligations", "invoices": len(invoices),
            "collected_invoice_records": len(credits), "outstanding_invoice_records": len(outstanding),
            "due_within_horizon_invoice_records": sum(row["due_period"] <= horizon for row in invoices.values()),
            "uncollected_due_invoice_records": sum(row["due_period"] <= horizon for row in outstanding),
            "future_due_invoice_records": sum(row["due_period"] > horizon for row in invoices.values()),
            "applied_collection_age_histogram": {str(age): count for age, count in sorted(ages.items())},
            "positive_applied_collection_age_histogram": {str(age): count for age, count in sorted(positive_ages.items())},
            "max_positive_origins_joined_per_allocation": max(positive_origins, default=0),
            "unapplied_collection_receipts": sum(a["artifact_id"] not in applied for a in receipts),
            "normal_collection_runs": sum(a["kind"] == "collection" for a in receipts),
            "dispute_resolution_runs": sum(a["kind"] == "dispute_resolution" for a in receipts),
            "max_collection_age_periods": max((int(row["period"][1:]) - int(row["origin_period"][1:]) for row in credits.values()), default=0),
            "max_origins_joined_per_collection": max(joined_origins, default=0),
            "max_origins_joined_per_allocation": max(allocation_origins, default=0),
            "cash_constrained_work_packages": len(constrained),
            "settled_periods": sorted({a["period"] for a in books}),
            "minimum_total_plan_claim": False,
            "scope": "Current recorded artifacts; collection ages and origin joins count only applied receipts. Not behavior success or proof every edge is decision-sensitive"}
