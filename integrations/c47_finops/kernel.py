"""Source-backed FinOps tools with exact bills, commitments and period lineage."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from integrations.internal_mcp.parameter_contract import apply_public_enums
from .build import KEYS
from .money import apportion, cents, commitment_schedule, instant


READS = {"list_organizations", "get_cloudhealth_dashboard", "list_cost_reports", "get_cost_report", "list_budgets", "get_budget_status",
    "list_commitments", "list_rightsizing_recommendations", "list_cloud_tags", "list_anomalies", "inspect_audit_trail"}


class BusinessError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise BusinessError(code)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


class FinOps:
    def __init__(self, task, runtime):
        self.task, self.runtime = Path(task).resolve(), Path(runtime).resolve()
        self.source = controller_source(self.task)
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Use an isolated runtime")
        self.backend = load_backend(self.source)
        self.logic = importlib.import_module("mcp_servers.cloudhealth_logic")
        self.public = read_json(self.task / "public/task.json")
        self.world = read_json(self.runtime / "world.json")
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        self.sm = TableState(self.world, read_json(self.task / "background.json"), KEYS)
        metadata = self.backend.get_tool_metadata()
        self.metadata = {name: deepcopy(metadata[name]) for name in sorted(READS) if name in metadata}
        for entry in self.metadata.values():
            entry["input_schema"]["additionalProperties"] = False
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 100}
        count = {"type": "integer", "minimum": 0}
        definitions = {
            "read_finops_period": ("Read current phase, public resource demand and prices for a billing period; usage is environment owned.", schema({"period_id": text}, [])),
            "read_resource": ("Read actual resource configuration and change history.", schema({"resource_id": text})),
            "list_allocation_mappings": ("Read active task tag predicates, percentages and revisions.", schema({})),
            "read_authorization_ledger": ("Read settled cash, current accrued cost and all remaining contractual obligations separately.", schema({})),
            "apply_tag_mapping": ("Update one real allocation predicate; reports become stale when their consumed rule changes.", schema({"organization_id": text,
                "tag_id": text, "tag_value": text, "business_unit_id": text, "allocation_basis_points": {"type": "integer", "minimum": 0, "maximum": 10000}, "actor_email": text})),
            "assess_resource_rightsizing": ("Assess the actor's proposed capacity against current public demand and resource revision.", schema({"resource_id": text, "target_size_id": text})),
            "read_capacity_assessment": ("Read an actual environment capacity assessment.", schema({"assessment_id": text})),
            "execute_rightsizing_action": ("Apply a passing current assessment in one unoccupied maintenance slot; never edits closed bills.", schema({"organization_id": text,
                "recommendation_id": text, "assessment_id": text, "maintenance_slot": count, "actor_email": text})),
            "record_commitment_purchase": ("Reserve the full contractual amount within remaining authorization. Current-period-only accounting is not allowed.", schema({"organization_id": text,
                "business_unit_id": text, "provider": {"enum": ["aws"]}, "service_category": {"enum": ["compute"]}, "start_date": text, "end_date": text,
                "hourly_commitment_usd": {"oneOf": [{"type": "number"}, {"type": "string"}]}, "actor_email": text})),
            "meter_current_period": ("Materialize an immutable bill from actual current resource configuration, demand and tags. Ends the planning phase.", schema({})),
            "read_metered_bill": ("Read an actual period bill and its resource/configuration source lines.", schema({"period_id": text})),
            "execute_cost_allocation_rebuild": ("Apply actual source tag rules to the current bill and account for commitment costs/credits. Repeated identical rebuild is idempotent.", schema({"organization_id": text,
                "report_month": text, "actor_email": text, "dry_run": {"type": "boolean"}}, ["organization_id", "report_month", "actor_email"])),
            "read_allocation": ("Read a created immutable allocation version, including historical stale versions.", schema({"allocation_id": text})),
            "acknowledge_anomaly": ("Acknowledge the task allocation anomaly without claiming that it is repaired.", schema({"organization_id": text, "anomaly_id": text, "acknowledged_by": text, "note": text}, ["organization_id", "anomaly_id", "acknowledged_by"])),
            "resolve_allocation_anomaly": ("Resolve the actual allocation gap using a current complete allocation, not a budget-label adjustment.", schema({"anomaly_id": text, "allocation_id": text})),
            "close_billing_period": ("Close exact current unit reports using the actual prior close and actor-computed expense; settles commitment portions without double counting.", schema({"period_id": text,
                "unit_report_ids": ids, "allocation_id": text, "previous_close_id": {"type": "string"}, "reported_expense_cents": count})),
            "read_period_close": ("Read a historical period close and its carry-forward balance/obligations.", schema({"close_id": text})),
            "poll_finops_updates": ("Poll the declared ownership change after the first close; modifies future resource tags only.", schema({})),
            "reconcile_organization_ledger": ("Join every actual period close and verify actor-reported unit totals, settled cash and remaining obligations.", schema({"close_ids": ids,
                "unit_totals_cents": {"type": "object", "additionalProperties": count}, "reported_settled_cents": count, "reported_remaining_commitment_cents": count})),
        }
        self.metadata.update({name: {"description": description, "input_schema": inputs} for name, (description, inputs) in definitions.items()})
        apply_public_enums("C47", self.metadata)
        self.validate()

    @property
    def state(self):
        return self.world["finops"]

    def now(self):
        return self.world["clock"]

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        from integrations.internal_mcp.topology import runtime_identifier
        return runtime_identifier(self.public, kind, value, f"{self.world['task_id']}-{kind}-{value}")

    def owned(self, kind, identifier):
        if kind == "resource":
            require(identifier in self.state["resources"], "RESOURCE_NOT_FOUND")
            return deepcopy(self.state["resources"][identifier])
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, kind.upper() + "_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def update(self, kind, identifier, values):
        return self.sm.update_entity(kind, identifier, values)

    def native(self, name, args):
        with patch.object(self.logic, "now_iso", self.now), patch.object(self.logic, "TARGET_TYPES", self.logic.TARGET_TYPES | {"resource", "assessment", "bill", "allocation", "period_close"}):
            result = self.backend.call_tool(name, state_manager=self.sm, **args)["content"]
        if result.get("message", "").startswith("INTERNAL_ERROR"):
            raise RuntimeError(result["message"])
        require(result["status"] == "success", result.get("message", "NATIVE_TOOL_ERROR"))
        return result["data"]

    def event(self, name, identifier, data):
        event = {"event_id": self.next_id("event"), "type": name, "entity_id": identifier,
            "sequence": self.world["sequence"], "occurred_at": self.now(), "data": deepcopy(data)}
        self.state["events"].append(event)
        target = {"mapping.revised": "tag_mapping", "capacity.assessed": "assessment", "capacity.executed": "resource", "ownership.changed": "resource",
            "commitment.purchased": "commitment", "usage.metered": "bill", "cost.allocated": "allocation", "anomaly.resolved": "anomaly", "period.closed": "period_close"}[name]
        self.sm.add_entity("audit_event", {"audit_event_id": event["event_id"] + "-audit", "organization_id": self.public["organization_id"],
            "actor_email": "environment@synthetic.invalid" if name in {"capacity.assessed", "ownership.changed", "usage.metered"} else self.public["actor_email"],
            "action": name, "target_type": target, "target_id": identifier, "result": "success", "before_summary": None,
            "after_summary": name + ": " + identifier, "request_id": event["event_id"], "created_at": self.now(), "business_event": deepcopy(event)})

    def current(self):
        require(self.state["period_index"] < len(self.public["periods"]), "ALL_PERIODS_CLOSED")
        return self.public["periods"][self.state["period_index"]]

    def period(self, identifier):
        matches = [p for p in self.public["periods"] if p["period_id"] == identifier]
        require(len(matches) == 1, "PERIOD_NOT_FOUND")
        return matches[0]

    def price(self, size_id):
        matches = [p for p in self.public["price_catalog"] if p["size_id"] == size_id]
        require(len(matches) == 1, "SIZE_NOT_FOUND")
        return matches[0]

    def current_allocation(self, identifier):
        require(identifier in self.state["allocations"], "ALLOCATION_NOT_FOUND")
        allocation = self.state["allocations"][identifier]
        require(all(self.owned("tag_mapping", r["tag_mapping_id"])["revision"] == r["revision"] for r in allocation["mapping_evidence"]), "STALE_ALLOCATION_RULES")
        return deepcopy(allocation)

    def ledger(self):
        closed_periods = {r["period_id"] for r in self.state["closes"].values()}
        settled = sum(r["period_expense_cents"] for r in self.state["closes"].values())
        remaining = sum(value for c in self.world["tables"]["commitment"] for period, value in c["period_charges_cents"].items() if period not in closed_periods)
        accrued = 0
        for period_id, bill in self.state["bills"].items():
            if period_id in closed_periods:
                continue
            accrued += sum(line["gross_cents"] for line in bill["lines"])
            current = [a for a in self.state["allocations"].values() if a["period_id"] == period_id]
            if current:
                try:
                    allocation = self.current_allocation(current[-1]["allocation_id"])
                    accrued -= sum(r["commitment_credit_cents"] for r in allocation["unit_totals"].values())
                except BusinessError:
                    pass
        return {"settled_cents": settled, "accrued_uncommitted_cents": accrued, "remaining_commitment_cents": remaining,
            "authorization_ceiling_cents": self.public["authorization_ceiling_cents"], "available_cents": self.public["authorization_ceiling_cents"] - settled - accrued - remaining}

    def dispatch(self, name, args):
        if name in READS:
            return self.read_native(name, args)
        if "organization_id" in args:
            require(args["organization_id"] == self.public["organization_id"], "ORGANIZATION_IDENTITY")
        if "actor_email" in args:
            require(args["actor_email"] == self.public["actor_email"], "ACTOR_IDENTITY")
        if "acknowledged_by" in args:
            require(args["acknowledged_by"] == self.public["actor_email"], "ACTOR_IDENTITY")
        if name == "read_finops_period":
            period = self.period(args["period_id"]) if args.get("period_id") else self.current()
            return {"period": period, "current_period_index": self.state["period_index"], "phase": self.state["phase"],
                "usage": [deepcopy(r) for r in self.state["usage"].values() if r["period_id"] == period["period_id"]],
                "resources": deepcopy(list(self.state["resources"].values())), "price_catalog": deepcopy(self.public["price_catalog"]),
                "recommendations": deepcopy(self.world["tables"]["rightsizing_recommendation"])}
        if name == "read_resource":
            return self.owned("resource", args["resource_id"])
        if name == "list_allocation_mappings":
            return {"mappings": deepcopy(self.world["tables"]["tag_mapping"])}
        if name == "read_authorization_ledger":
            return self.ledger()
        if name == "apply_tag_mapping":
            require(self.state["phase"] != "complete", "ALL_PERIODS_CLOSED")
            require(args["tag_id"] == self.public["tag_id"] and args["tag_value"] in self.public["allocation_policy"], "TAG_PREDICATE_IDENTITY")
            self.owned("business_unit", args["business_unit_id"])
            rows = [m for m in self.world["tables"]["tag_mapping"] if m["tag_id"] == args["tag_id"] and m["tag_value"] == args["tag_value"] and m["business_unit_id"] == args["business_unit_id"]]
            require(len(rows) == 1, "MAPPING_IDENTITY_NOT_IN_TASK")
            row = deepcopy(rows[0])
            if row["allocation_basis_points"] == args["allocation_basis_points"]:
                return {"mapping": deepcopy(row), "idempotent": True}
            updated = self.update("tag_mapping", row["tag_mapping_id"], {"allocation_basis_points": args["allocation_basis_points"],
                "allocation_percent": args["allocation_basis_points"] / 100, "revision": row["revision"] + 1, "updated_at": self.now()})
            self.event("mapping.revised", row["tag_mapping_id"], {"previous": row, "updated": updated})
            self.refresh_tag_coverage()
            return {"mapping": updated, "idempotent": False}
        if name == "assess_resource_rightsizing":
            require(self.state["phase"] == "planning", "PERIOD_ALREADY_METERED")
            period = self.current()
            resource = self.owned("resource", args["resource_id"])
            require(resource["service_category"] == "compute", "RESOURCE_NOT_RESIZABLE")
            option = self.price(args["target_size_id"])
            require(option["size_id"] != "storage", "RESOURCE_SIZE_FAMILY")
            usage = next(r for r in self.state["usage"].values() if r["period_id"] == period["period_id"] and r["resource_id"] == resource["resource_id"])
            identifier = self.next_id("assessment")
            assessment = {"assessment_id": identifier, "period_id": period["period_id"], "resource_id": resource["resource_id"], "resource_version": resource["version"],
                "target_size_id": option["size_id"], "required_capacity": usage["required_capacity"], "target_capacity": option["capacity"],
                "passed": option["capacity"] >= usage["required_capacity"], "current_hourly_cents": self.price(resource["size_id"])["hourly_cents"],
                "target_hourly_cents": option["hourly_cents"], "assessed_at": self.now(), **self.capacity_context(usage)}
            self.state["assessments"][identifier] = assessment
            self.event("capacity.assessed", identifier, assessment)
            return deepcopy(assessment)
        if name == "read_capacity_assessment":
            require(args["assessment_id"] in self.state["assessments"], "ASSESSMENT_NOT_FOUND")
            return deepcopy(self.state["assessments"][args["assessment_id"]])
        if name == "execute_rightsizing_action":
            return self.execute_capacity(args)
        if name == "record_commitment_purchase":
            return self.purchase(args)
        if name == "meter_current_period":
            return self.meter()
        if name == "read_metered_bill":
            require(args["period_id"] in self.state["bills"], "BILL_NOT_METERED")
            return deepcopy(self.state["bills"][args["period_id"]])
        if name == "execute_cost_allocation_rebuild":
            require(args["report_month"] == self.current()["period_id"] and self.state["phase"] == "metered", "CURRENT_METERED_PERIOD_REQUIRED")
            return self.allocate(args["report_month"], args.get("dry_run", False))
        if name == "read_allocation":
            require(args["allocation_id"] in self.state["allocations"], "ALLOCATION_NOT_FOUND")
            return deepcopy(self.state["allocations"][args["allocation_id"]])
        if name == "acknowledge_anomaly":
            self.owned("anomaly", args["anomaly_id"])
            return self.native(name, args)
        if name == "resolve_allocation_anomaly":
            anomaly = self.owned("anomaly", args["anomaly_id"])
            allocation = self.current_allocation(args["allocation_id"])
            require(allocation["period_id"] == self.current()["period_id"], "CURRENT_ALLOCATION_REQUIRED")
            result = self.update("anomaly", anomaly["anomaly_id"], {"status": "resolved", "resolved_by": self.public["actor_email"], "resolution_allocation_id": allocation["allocation_id"]})
            self.event("anomaly.resolved", anomaly["anomaly_id"], {"allocation_id": allocation["allocation_id"]})
            return result
        if name == "close_billing_period":
            return self.close(args)
        if name == "read_period_close":
            require(args["close_id"] in self.state["closes"], "CLOSE_NOT_FOUND")
            return deepcopy(self.state["closes"][args["close_id"]])
        if name == "poll_finops_updates":
            return self.poll_updates()
        if name == "reconcile_organization_ledger":
            return self.reconcile(args)
        raise BusinessError("TOOL_NOT_EXPOSED")

    def read_native(self, name, args):
        actual = deepcopy(args)
        task_scope = args.get("organization_id") == self.public["organization_id"]
        if name == "get_cloudhealth_dashboard" and task_scope and args.get("report_month", "latest") == "latest":
            actual["report_month"] = list(self.state["bills"])[-1] if self.state["bills"] else self.current()["period_id"]
        data = self.native(name, actual)
        if name == "list_commitments":
            for row in data["commitments"]:
                if row.get("task_id") == self.world["task_id"]:
                    row["days_until_end"] = (instant(row["end_date"]) - instant(self.now())).days
                    row["utilization_percent"] = round(row.get("applied_credit_cents", 0) / (self.public["commitment_credit_multiplier"] * row["total_value_cents"]) * 100, 2) if row["total_value_cents"] else 0
        if name == "get_cloudhealth_dashboard" and task_scope:
            month = data["report_month"]
            data["commitment_summary"] = {"active_count": sum(c["status"] == "active" for c in self.world["tables"]["commitment"]),
                "period_charge_cents": sum(c["period_charges_cents"].get(month, 0) for c in self.world["tables"]["commitment"]),
                "remaining_obligation_cents": self.ledger()["remaining_commitment_cents"]}
            data["authorization"] = self.ledger()
            data["accounting_scope"] = "exact_selected_period_and_whole_horizon_authorization"
        return data

    def refresh_tag_coverage(self):
        covered = 0
        for resource in self.state["resources"].values():
            weights = {m["business_unit_id"]: m["allocation_basis_points"] for m in self.world["tables"]["tag_mapping"]
                if m["tag_id"] == resource["tag_id"] and m["tag_value"] == resource["tag_value"] and m["status"] == "active"}
            covered += weights == self.public["allocation_policy"][resource["tag_value"]]
        coverage = round(covered / len(self.state["resources"]) * 100, 2)
        self.update("cloud_tag", self.public["tag_id"], {"coverage_percent": coverage})
        self.update("cloud_account", self.public["cloud_account_id"], {"tag_compliance_score": coverage})

    def capacity_context(self, usage):
        return {}

    def execute_capacity(self, args):
        require(self.state["phase"] == "planning", "PERIOD_ALREADY_METERED")
        require(args["assessment_id"] in self.state["assessments"], "ASSESSMENT_NOT_FOUND")
        assessment = self.state["assessments"][args["assessment_id"]]
        recommendation = self.owned("rightsizing_recommendation", args["recommendation_id"])
        require(recommendation["resource_id"] == assessment["resource_id"], "RECOMMENDATION_RESOURCE_IDENTITY")
        require(assessment["period_id"] == self.current()["period_id"] and assessment["passed"], "CURRENT_PASSING_ASSESSMENT_REQUIRED")
        resource = self.owned("resource", assessment["resource_id"])
        prior = [e for e in self.state["events"] if e["type"] == "capacity.executed" and e["data"]["assessment_id"] == args["assessment_id"]]
        if prior:
            return {"resource": resource, "idempotent": True}
        require(resource["version"] == assessment["resource_version"], "STALE_CAPACITY_ASSESSMENT")
        if resource["size_id"] == assessment["target_size_id"]:
            return {"resource": resource, "idempotent": True, "no_configuration_change": True}
        require(args["maintenance_slot"] < self.public["maintenance_slots_per_period"], "MAINTENANCE_SLOT_RANGE")
        slot = f"{assessment['period_id']}:{args['maintenance_slot']}"
        require(slot not in self.state["maintenance"], "MAINTENANCE_OCCUPIED")
        self.state["maintenance"][slot] = resource["resource_id"]
        change = {"period_id": assessment["period_id"], "assessment_id": args["assessment_id"], "old_size_id": resource["size_id"],
            "new_size_id": assessment["target_size_id"], "previous_version": resource["version"], "new_version": resource["version"] + 1, "maintenance_slot": args["maintenance_slot"]}
        resource.update(size_id=assessment["target_size_id"], version=resource["version"] + 1)
        resource["change_history"].append(change)
        self.state["resources"][resource["resource_id"]] = resource
        self.update("rightsizing_recommendation", recommendation["recommendation_id"], {"status": "executed", "current_size": resource["size_id"],
            "recommended_size": resource["size_id"], "actioned_at": self.now(), "monthly_savings_usd": 0})
        self.event("capacity.executed", resource["resource_id"], change)
        return {"resource": deepcopy(resource), "idempotent": False}

    def purchase(self, args):
        require(self.state["phase"] == "planning", "RETROACTIVE_COMMITMENT_FORBIDDEN")
        self.owned("business_unit", args["business_unit_id"])
        try:
            hourly = cents(args["hourly_commitment_usd"])
            total, charges = commitment_schedule(hourly, args["start_date"], args["end_date"], self.public["periods"])
            require(instant(args["start_date"]) >= instant(self.current()["start"]), "COMMITMENT_START_IN_CLOSED_HISTORY")
        except (ValueError, TypeError, AttributeError):
            raise BusinessError("INVALID_COMMITMENT_AMOUNT_OR_INTERVAL") from None
        require(hourly in self.public["commitment_hourly_cent_options"], "COMMITMENT_OFFER_NOT_AVAILABLE")
        for commitment in self.world["tables"]["commitment"]:
            if commitment["business_unit_id"] == args["business_unit_id"] and commitment["service_category"] == args["service_category"]:
                require(instant(args["end_date"]) <= instant(commitment["start_date"]) or instant(args["start_date"]) >= instant(commitment["end_date"]), "OVERLAPPING_COVERAGE")
        require(total <= self.ledger()["available_cents"], "INSUFFICIENT_FULL_HORIZON_AUTHORIZATION")
        identifier = self.next_id("commitment")
        record = {"commitment_id": identifier, "organization_id": self.public["organization_id"], "business_unit_id": args["business_unit_id"], "provider": args["provider"],
            "service_category": args["service_category"], "commitment_type": "savings_plan", "start_date": args["start_date"], "end_date": args["end_date"],
            "hourly_commitment_cents": hourly, "hourly_commitment_usd": hourly / 100, "total_value_cents": total, "total_value_usd": total / 100,
            "period_charges_cents": charges, "utilized_value_usd": 0, "applied_credit_cents": 0, "settled_charge_cents": 0, "remaining_obligation_cents": total,
            "coverage_target_percent": 0, "current_coverage_percent": 0, "exchange_eligible": False,
            "status": "active", "created_at": self.now(), "updated_at": self.now()}
        self.sm.add_entity("commitment", record)
        self.event("commitment.purchased", identifier, {"reserved_cents": total, "period_charges_cents": charges})
        return {"commitment": self.owned("commitment", identifier), "authorization": self.ledger()}

    def meter(self):
        period = self.current()
        if period["period_id"] in self.state["bills"]:
            return deepcopy(self.state["bills"][period["period_id"]])
        require(self.state["phase"] == "planning", "NOT_PLANNING")
        if self.state["period_index"] > 0:
            require(len(self.state["applied_updates"]) == len(self.state["scheduled_updates"]), "DECLARED_UPDATE_NOT_POLLED")
        lines = []
        for usage in self.state["usage"].values():
            if usage["period_id"] != period["period_id"]:
                continue
            resource = self.owned("resource", usage["resource_id"])
            price = self.price(resource["size_id"])
            require(price["capacity"] >= usage["required_capacity"], "RESOURCE_CAPACITY_BELOW_DEMAND")
            lines.append({"usage_id": usage["usage_id"], "resource": resource, "billable_hours": usage["billable_hours"],
                "required_capacity": usage["required_capacity"], "hourly_cents": price["hourly_cents"], "gross_cents": usage["billable_hours"] * price["hourly_cents"]})
        bill = {"bill_id": self.next_id("bill"), "period_id": period["period_id"], "lines": lines, "metered_at": period["end"],
            "gross_cents": sum(r["gross_cents"] for r in lines)}
        self.state["bills"][period["period_id"]] = bill
        self.state["phase"] = "metered"
        self.world["clock"] = period["end"]
        self.update("cloud_account", self.public["cloud_account_id"], {"monthly_spend_usd": bill["gross_cents"] / 100, "spend_report_month": period["period_id"]})
        self.event("usage.metered", bill["bill_id"], {"period_id": period["period_id"], "gross_cents": bill["gross_cents"]})
        return deepcopy(bill)

    def allocate(self, period_id, dry_run):
        bill = self.state["bills"][period_id]
        evidence = {}
        unit_totals = {u: {"gross_cents": 0, "compute_gross_cents": 0, "commitment_charge_cents": 0, "commitment_credit_cents": 0, "expense_cents": 0} for u in self.public["unit_ids"]}
        lines = []
        for line in bill["lines"]:
            resource = line["resource"]
            mappings = [m for m in self.world["tables"]["tag_mapping"] if m["tag_id"] == resource["tag_id"] and m["tag_value"] == resource["tag_value"] and m["status"] == "active"]
            weights = {m["business_unit_id"]: m["allocation_basis_points"] for m in mappings}
            require(len(weights) == len(mappings) and sum(weights.values()) == 10000, "ALLOCATION_PERCENTAGES_NOT_CONSERVED")
            require(weights == self.public["allocation_policy"][resource["tag_value"]], "ALLOCATION_OWNER_POLICY_MISMATCH")
            split = apportion(line["gross_cents"], weights)
            lines.append({"usage_id": line["usage_id"], "gross_cents": line["gross_cents"], "unit_charges_cents": split,
                "mapping_ids": sorted(m["tag_mapping_id"] for m in mappings)})
            for mapping in mappings:
                evidence[mapping["tag_mapping_id"]] = deepcopy(mapping)
            for unit, amount in split.items():
                unit_totals[unit]["gross_cents"] += amount
                if resource["service_category"] == "compute":
                    unit_totals[unit]["compute_gross_cents"] += amount
        coverage = []
        for commitment in self.world["tables"]["commitment"]:
            charge = commitment["period_charges_cents"][period_id]
            unit = unit_totals[commitment["business_unit_id"]]
            credit = min(unit["compute_gross_cents"] - unit["commitment_credit_cents"], self.public["commitment_credit_multiplier"] * charge)
            unit["commitment_charge_cents"] += charge
            unit["commitment_credit_cents"] += credit
            coverage.append({"commitment_id": commitment["commitment_id"], "charge_cents": charge, "credit_cents": credit})
        for unit in unit_totals.values():
            unit["expense_cents"] = unit["gross_cents"] - unit["commitment_credit_cents"] + unit["commitment_charge_cents"]
        content = {"period_id": period_id, "bill_id": bill["bill_id"], "lines": lines, "mapping_evidence": [evidence[k] for k in sorted(evidence)],
            "unit_totals": unit_totals, "coverage": coverage, "period_expense_cents": sum(u["expense_cents"] for u in unit_totals.values())}
        if dry_run:
            return {**content, "dry_run": True}
        for existing in self.state["allocations"].values():
            if all(existing.get(k) == value for k, value in content.items()):
                return deepcopy(existing)
        identifier = self.next_id("allocation")
        allocation = {**content, "allocation_id": identifier, "created_at": self.now()}
        self.state["allocations"][identifier] = allocation
        self.update("cloud_account", self.public["cloud_account_id"], {"monthly_spend_usd": content["period_expense_cents"] / 100, "spend_report_month": period_id})
        for report in self.world["tables"]["cost_report"]:
            if report["report_month"] == period_id:
                require(report["status"] != "locked", "CLOSED_REPORT_IMMUTABLE")
                totals = unit_totals[report["business_unit_id"]]
                self.update("cost_report", report["cost_report_id"], {"status": "published", "source_allocation_id": identifier,
                    "gross_cost_usd": totals["gross_cents"] / 100, "allocated_cost_usd": totals["expense_cents"] / 100, "untagged_cost_usd": 0,
                    "amortized_commitment_credit_usd": totals["commitment_credit_cents"] / 100, "expense_cents": totals["expense_cents"], "generated_at": self.now()})
        self.event("cost.allocated", identifier, {"period_id": period_id, "expense_cents": content["period_expense_cents"]})
        return deepcopy(allocation)

    def close(self, args):
        period = self.current()
        require(self.state["phase"] == "metered" and args["period_id"] == period["period_id"], "CURRENT_METERED_PERIOD_REQUIRED")
        allocation = self.current_allocation(args["allocation_id"])
        require(allocation["period_id"] == period["period_id"], "WRONG_PERIOD_ALLOCATION")
        reports = [r for r in self.world["tables"]["cost_report"] if r["report_month"] == period["period_id"]]
        require(set(args["unit_report_ids"]) == {r["cost_report_id"] for r in reports} and len(reports) == len(self.public["unit_ids"]), "UNIT_REPORT_JOIN_MISMATCH")
        require(all(r["source_allocation_id"] == allocation["allocation_id"] and r["status"] == "published" for r in reports), "STALE_UNIT_REPORT")
        require(args["reported_expense_cents"] == allocation["period_expense_cents"], "REPORTED_EXPENSE_MISMATCH")
        prior = list(self.state["closes"].values())
        require(args["previous_close_id"] == (prior[-1]["close_id"] if prior else ""), "PREVIOUS_PERIOD_CLOSE_MISMATCH")
        before = self.ledger()
        require(before["available_cents"] >= 0, "AUTHORIZATION_EXCEEDED")
        require(all(a["status"] == "resolved" for a in self.world["tables"]["anomaly"]), "UNRESOLVED_ALLOCATION_ANOMALY")
        identifier = self.next_id("close")
        charge = sum(u["commitment_charge_cents"] for u in allocation["unit_totals"].values())
        record = {"close_id": identifier, "period_id": period["period_id"], "previous_close_id": args["previous_close_id"], "allocation_id": allocation["allocation_id"],
            "unit_report_ids": sorted(args["unit_report_ids"]), "unit_expense_cents": {u: t["expense_cents"] for u, t in allocation["unit_totals"].items()},
            "opening_settled_cents": before["settled_cents"], "period_expense_cents": allocation["period_expense_cents"],
            "closing_settled_cents": before["settled_cents"] + allocation["period_expense_cents"], "remaining_commitment_cents": before["remaining_commitment_cents"] - charge,
            "settled_commitment_cents": charge, "closed_at": self.now()}
        self.state["closes"][identifier] = record
        for report in reports:
            self.update("cost_report", report["cost_report_id"], {"status": "locked", "close_id": identifier})
        self.state["period_index"] += 1
        self.state["phase"] = "complete" if self.state["period_index"] == len(self.public["periods"]) else "planning"
        closed = {c["period_id"] for c in self.state["closes"].values()}
        for commitment in self.world["tables"]["commitment"]:
            paid = sum(value for month, value in commitment["period_charges_cents"].items() if month in closed)
            used = sum(item["credit_cents"]
                for c in self.state["closes"].values() for item in self.state["allocations"][c["allocation_id"]]["coverage"] if item["commitment_id"] == commitment["commitment_id"])
            self.update("commitment", commitment["commitment_id"], {"settled_charge_cents": paid,
                "remaining_obligation_cents": commitment["total_value_cents"] - paid,
                "applied_credit_cents": used, "utilized_value_usd": used / 100,
                "status": "expired" if instant(commitment["end_date"]) <= instant(self.now()) else "active"})
        for budget in self.world["tables"]["budget"]:
            actual = sum(c["unit_expense_cents"][budget["business_unit_id"]] for c in self.state["closes"].values())
            self.update("budget", budget["budget_id"], {"actual_spend_usd": actual / 100, "forecast_spend_usd": actual / 100,
                "status": "exceeded" if actual > cents(budget["amount_usd"]) else "active"})
        self.event("period.closed", identifier, record)
        return deepcopy(record)

    def poll_updates(self):
        if self.state["applied_updates"]:
            return {"status": "drained", "events": []}
        require(self.state["phase"] == "planning", "PLANNING_PHASE_REQUIRED")
        if not self.state["closes"]:
            return {"status": "awaiting_first_close", "events": []}
        update = self.state["scheduled_updates"][0]
        resource = self.owned("resource", update["resource_id"])
        resource.update(tag_value=update["tag_value"], version=resource["version"] + 1)
        self.state["resources"][resource["resource_id"]] = resource
        released = {**update, "resource_version": resource["version"], "effective_period_id": self.current()["period_id"], "released_at": self.now()}
        self.refresh_tag_coverage()
        self.state["applied_updates"].append(released)
        self.event("ownership.changed", resource["resource_id"], released)
        return {"status": "update_released", "events": [deepcopy(released)]}

    def reconcile(self, args):
        require(self.state["phase"] == "complete", "BILLING_HORIZON_NOT_CLOSED")
        require(set(args["close_ids"]) == set(self.state["closes"]) and len(args["close_ids"]) == len(self.public["periods"]), "PERIOD_CLOSE_JOIN_MISMATCH")
        require(len(self.state["applied_updates"]) == len(self.state["scheduled_updates"]), "PLANNED_UPDATES_INCOMPLETE")
        totals = {u: sum(c["unit_expense_cents"][u] for c in self.state["closes"].values()) for u in self.public["unit_ids"]}
        ledger = self.ledger()
        require(args["unit_totals_cents"] == totals and args["reported_settled_cents"] == ledger["settled_cents"]
            and args["reported_remaining_commitment_cents"] == ledger["remaining_commitment_cents"], "ORGANIZATION_RECONCILIATION_MISMATCH")
        require(ledger["available_cents"] >= 0 and ledger["accrued_uncommitted_cents"] == 0, "LEDGER_NOT_BALANCED")
        record = {"close_ids": sorted(args["close_ids"]), "unit_totals_cents": totals, "ledger": ledger, "completed_at": self.now()}
        self.state["final_reconciliation"] = record
        return deepcopy(record)

    def validate(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows), "DUPLICATE_ENTITY_ID")
            require(all(r.get("task_id") == self.world["task_id"] for r in rows), "TASK_OWNERSHIP")
        for commitment in self.world["tables"]["commitment"]:
            require(sum(commitment["period_charges_cents"].values()) == commitment["total_value_cents"], "COMMITMENT_CHARGE_CONSERVATION")
        for close in self.state["closes"].values():
            require(sum(close["unit_expense_cents"].values()) == close["period_expense_cents"], "UNIT_CLOSE_CONSERVATION")
            require(close["opening_settled_cents"] + close["period_expense_cents"] == close["closing_settled_cents"], "CLOSE_BALANCE_CONSERVATION")

    def record_call(self, name, arguments, result):
        pass

    def call(self, name, arguments):
        before = deepcopy(self.world)
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.validate()
            result = {"status": "success", "data": data, "message": ""}
        except BusinessError as error:
            self.world = before
            self.sm.bind(self.world)
            result = {"status": "error", "data": {}, "message": str(error)}
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        self.record_call(name, arguments, result)
        self.world["sequence"] += 1
        try:
            write_json(self.runtime / "world.json", self.world)
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        return result
