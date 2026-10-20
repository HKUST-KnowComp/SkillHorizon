"""Build connected resource, allocation and multi-period commitment tasks."""

import argparse
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json
from .money import apportion, hours


KEYS = {"organization": "organization_id", "business_unit": "business_unit_id", "cloud_account": "cloud_account_id",
    "cost_report": "cost_report_id", "budget": "budget_id", "commitment": "commitment_id", "rightsizing_recommendation": "recommendation_id",
    "cloud_tag": "tag_id", "tag_mapping": "tag_mapping_id", "anomaly": "anomaly_id", "audit_event": "audit_event_id"}
FORMAT = "skillhorizon.c47.period_ledger.v1"
SKILLS = Path(__file__).with_name("skills")


def build(source, output, *, seed=47, units=2, periods=3, resources_per_unit=2, background=True, workload_network=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use fresh isolated output")
    if type(seed) is not int or type(units) is not int or not 2 <= units <= 3 or type(periods) is not int or not 2 <= periods <= 6 or type(resources_per_unit) is not int or not 1 <= resources_per_unit <= 3:
        raise ValueError("Use integer seed, 2..3 units, 2..6 periods and 1..3 resources per unit")
    original = {kind: read_json(source / f"data/database/{'anomalies' if kind == 'anomaly' else kind + 's'}.json") for kind in KEYS}
    rng = random.Random(seed)
    task_id = f"c47-{'workload' if workload_network else 'finops'}-{seed}-{units}u-{periods}p-{resources_per_unit}r"
    organization, account, tag = (task_id + suffix for suffix in ("-org", "-cloud", "-cost-center"))
    actor = "coordinator@synthetic.invalid"
    tables = {kind: [] for kind in KEYS}

    def add(kind, identifier, values):
        row = {**deepcopy(rng.choice(original[kind])), **values, KEYS[kind]: identifier, "task_id": task_id, "created_at": origin, "updated_at": origin}
        tables[kind].append(row)
        return row

    calendar = []
    for index in range(periods):
        month = 10 + index
        year, month = 2026 + (month - 1) // 12, (month - 1) % 12 + 1
        next_year, next_month = (year + 1, 1) if month == 12 else (year, month + 1)
        calendar.append({"period_id": f"{year}-{month:02d}", "start": datetime(year, month, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z"),
            "end": datetime(next_year, next_month, 1, tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")})
    origin = calendar[0]["start"]
    add("organization", organization, {"name": "Shared obligation FinOps", "currency": "USD", "status": "active", "finops_owner": actor,
        "billing_month_start_day": 1, "primary_domain": "synthetic.invalid"})
    unit_ids = [f"{task_id}-unit{i}" for i in range(units)]
    for index, identifier in enumerate(unit_ids):
        add("business_unit", identifier, {"organization_id": organization, "name": f"Business unit {index + 1}", "active": True,
            "owner_email": f"owner{index}@synthetic.invalid", "cost_center": f"unit{index}", "parent_business_unit_id": None, "monthly_budget_usd": 0})
    add("cloud_account", account, {"organization_id": organization, "business_unit_id": unit_ids[0], "account_name": "Shared AWS synthetic account",
        "provider": "aws", "region": "synthetic-region", "status": "active", "monthly_spend_usd": 0, "tag_compliance_score": 0})
    tag_values = [f"unit{i}" for i in range(units)] + ["shared"]
    add("cloud_tag", tag, {"organization_id": organization, "key": "cost-center", "governance_status": "required", "allowed_values": tag_values, "coverage_percent": 0})
    shared = {identifier: 10000 // units for identifier in unit_ids}
    shared[unit_ids[0]] += 10000 - sum(shared.values())
    allocation_policy = {f"unit{i}": {identifier: 10000} for i, identifier in enumerate(unit_ids)}
    allocation_policy["shared"] = shared
    for value, split in allocation_policy.items():
        for identifier, basis_points in split.items():
            mapping_id = f"{task_id}-map-{value}-{identifier.rsplit('unit', 1)[-1]}"
            add("tag_mapping", mapping_id, {"organization_id": organization, "business_unit_id": identifier, "cloud_account_id": None,
                "tag_id": tag, "tag_value": value, "allocation_percent": basis_points / 200,
                "allocation_basis_points": basis_points // 2, "status": "active", "revision": 1, "last_applied_at": None})
    catalog = [{"size_id": "small", "capacity": 2, "hourly_cents": 4}, {"size_id": "compact", "capacity": 4, "hourly_cents": 7},
        {"size_id": "balanced", "capacity": 6, "hourly_cents": 9}, {"size_id": "large", "capacity": 8, "hourly_cents": 12}]
    resources, usage = {}, {}
    for lane, unit in enumerate(unit_ids):
        for index in range(resources_per_unit):
            resource_id = f"{task_id}-resource-{lane}-{index}"
            resources[resource_id] = {"resource_id": resource_id, "organization_id": organization, "cloud_account_id": account,
                "business_unit_id": unit, "provider": "aws", "service_category": "compute", "tag_id": tag, "tag_value": f"unit{lane}",
                "size_id": "large", "version": 1, "change_history": []}
            for period in calendar:
                identifier = f"{resource_id}-{period['period_id']}"
                usage[identifier] = {"usage_id": identifier, "resource_id": resource_id, "period_id": period["period_id"],
                    "billable_hours": int(hours(period["start"], period["end"])), "required_capacity": rng.choice((3, 4, 5)), "source": "environment_synthetic_meter"}
            recommendation = f"{resource_id}-recommendation"
            add("rightsizing_recommendation", recommendation, {"organization_id": organization, "business_unit_id": unit, "cloud_account_id": account,
                "provider": "aws", "service_category": "compute", "resource_id": resource_id, "resource_name": resource_id, "resource_type": "vm",
                "current_size": "large", "recommended_size": "inspect_current_demand", "requires_approval": True, "risk_level": "medium", "status": "open",
                "monthly_savings_usd": 0, "linked_commitment_id": None, "actioned_at": None})
    shared_id = task_id + "-resource-shared"
    resources[shared_id] = {"resource_id": shared_id, "organization_id": organization, "cloud_account_id": account, "business_unit_id": unit_ids[0],
        "provider": "aws", "service_category": "storage", "tag_id": tag, "tag_value": "shared", "size_id": "storage", "version": 1, "change_history": []}
    for period in calendar:
        identifier = shared_id + "-" + period["period_id"]
        usage[identifier] = {"usage_id": identifier, "resource_id": shared_id, "period_id": period["period_id"],
            "billable_hours": int(hours(period["start"], period["end"])), "required_capacity": 1, "source": "environment_synthetic_meter"}
    catalog.append({"size_id": "storage", "capacity": 1, "hourly_cents": 3})
    baseline = sum(row["billable_hours"] * (3 if resources[row["resource_id"]]["service_category"] == "storage" else
        min(c["hourly_cents"] for c in catalog if c["size_id"] != "storage" and c["capacity"] >= row["required_capacity"])) for row in usage.values())
    ceiling = baseline + baseline // 8
    budget_shares = apportion(ceiling, {u: 1 for u in unit_ids})
    for unit in unit_ids:
        budget_id = unit + "-budget"
        add("budget", budget_id, {"organization_id": organization, "business_unit_id": unit, "name": "Fixed whole-horizon planning share",
            "scope_type": "business_unit", "scope_ref_id": unit, "period": "annual", "amount_usd": budget_shares[unit] / 100,
            "actual_spend_usd": 0, "forecast_spend_usd": 0, "status": "active", "owner_email": actor,
            "warning_threshold_percent": 80, "critical_threshold_percent": 100, "rollover_enabled": False})
        for period in calendar:
            report_id = f"{unit}-{period['period_id']}-report"
            add("cost_report", report_id, {"organization_id": organization, "business_unit_id": unit, "cloud_account_id": account,
                "report_month": period["period_id"], "currency": "USD", "service_category": "platform", "status": "draft",
                "gross_cost_usd": 0, "allocated_cost_usd": 0, "untagged_cost_usd": 0, "amortized_commitment_credit_usd": 0,
                "expense_cents": 0, "source_allocation_id": None, "variance_percent": 0, "generated_at": None})
    anomaly_id = task_id + "-allocation-gap"
    add("anomaly", anomaly_id, {"organization_id": organization, "business_unit_id": unit_ids[0], "cloud_account_id": account,
        "cost_report_id": tables["cost_report"][0]["cost_report_id"], "status": "open", "severity": "high", "service_category": "platform",
        "suspected_cause": "Active source mapping percentages do not conserve all charges.", "resolved_by": None, "acknowledged_by": None,
        "baseline_cost_usd": 0, "actual_cost_usd": 0, "variance_usd": 0, "linked_budget_id": None, "detected_at": origin})
    change = {"event_id": task_id + "-shared-ownership-update", "resource_id": next(iter(resources)), "tag_value": "shared",
        "after_period": calendar[0]["period_id"], "reason": "One compute resource now serves shared organization workloads; future costs must use the shared allocation rule."}
    skills = [{"name": p.name, "files": [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in {".md", ".py"}]}
        for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "organization_id": organization, "cloud_account_id": account, "actor_email": actor,
        "unit_ids": unit_ids, "periods": calendar, "resource_ids": list(resources), "price_catalog": catalog, "tag_id": tag,
        "allocation_policy": allocation_policy, "authorization_ceiling_cents": ceiling, "maintenance_slots_per_period": len(resources),
        "goal": "Repair source allocation, choose capacity-safe configurations and affordable commitment coverage, meter and close every period, "
            "then reconcile all unit reports and the organization ledger. Carry prior close balances and future contractual obligations correctly. "
            "Poll the declared ownership update after the first close. Alternative feasible portfolios are allowed; limits cannot be raised.",
        "commitment_credit_multiplier": 2, "commitment_hourly_cent_options": [2, 4, 6, 24], "planned_updates": 1, "skills": skills,
        "policy": {"simulation": "synthetic_capacity_and_cost_not_live_cloud_performance", "currency": "USD",
            "skill_protocol": "observational_v1", "accounting": "settled_cash_plus_accrued_uncommitted_cost_plus_remaining_contractual_obligations",
            "unpaid_commitments_are_reserved": True, "closed_history_immutable": True}}
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "sequence": 0, "clock": origin,
        "finops": {"next_id": 1, "period_index": 0, "phase": "planning", "resources": resources, "usage": usage,
            "assessments": {}, "maintenance": {}, "bills": {}, "allocations": {}, "closes": {}, "final_reconciliation": None,
            "scheduled_updates": [change], "applied_updates": [], "events": [], "skill_runs": {}, "active_skill_run": None}}
    write_json(output / "background.json", original if background else {kind: [] for kind in KEYS})
    write_json(output / "public/task.json", public)
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "resources": resources, "usage": usage, "scheduled_updates": [change]})
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c47-finops": {"command": sys.executable,
        "args": ["-m", "integrations.c47_finops.server", str(output), str(output / "runtime")], "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=47)
    parser.add_argument("--units", type=int, default=2)
    parser.add_argument("--periods", type=int, default=3)
    parser.add_argument("--resources-per-unit", type=int, default=2)
    args = parser.parse_args()
    print(build(args.source, args.output, seed=args.seed, units=args.units, periods=args.periods, resources_per_unit=args.resources_per_unit)["task_id"])
