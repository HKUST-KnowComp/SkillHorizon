"""Offline effective business dependencies and recovery, not agent feedback."""
from collections import Counter


def horizon_metrics(runtime):
    p = runtime.project
    extra = {}
    if "services" in p:
        records = [a["data"] for a in p["artifacts"].values() if a["kind"] == "service_commission"]
        ages = [d["stage"]-p["artifacts"][key]["data"]["stage"] for d in records for key in d["upstream_service_ids"]]
        extra = {"service_commissions": len(records), "active_services": sum(s["active"] for s in p["services"].values()),
            "reserved_flow_units": sum(s["flow_units"] for s in p["services"].values()),
            "shared_flow_capacity": p["service_rules"]["capacity_units"], "service_input_stage_gaps": dict(Counter(ages)),
            "service_return_pressures": {key: p["artifacts"][s["current_id"]]["data"]["return_pressure"]
                                         for key,s in p["services"].items() if s["current_id"]}}
    if "assembly_rules" in p:
        kits = [a["data"] for a in p["artifacts"].values() if a["kind"] == "contract_kit"]
        ages = Counter()
        for d in kits:
            for row in d["inputs"]:
                ages[d["stage"]-p["requests"][row["request_id"]]["stage"]] += row["quantity"]
        conditions = [a["data"] for a in p["artifacts"].values() if a["kind"] == "conditioning"]
        extra = {"assembled_kits": len(kits), "consumed_material_units_by_stage_age": dict(ages),
            "max_material_origin_join": max((len(d["origin_quantities"]) for d in kits), default=0),
            "conditioning_operations": len(conditions), "conditioning_work_units": sum(d["work_units"] for d in conditions),
            "conditioning_cost_cents": sum(d["cost_cents"] for d in conditions)}
    if "network" in p:
        tests = [a["data"] for a in p["artifacts"].values() if a["kind"] == "test"]
        reused = [t for t in tests if t["isolation_id"] != p["artifacts"][t["repair_id"]]["data"]["isolation_id"]]
        return {**extra, "profile": p["business_program"], "devices": len(p["devices"]),
                "unfinished_isolation_releases": sum(a["kind"] == "isolation_release" for a in p["artifacts"].values()),
                "physical_repairs": sum(a["kind"] == "repair" for a in p["artifacts"].values()),
                "test_measurements": len(tests), "retests_reusing_physical_repairs": len(reused),
                "spent_spares": sum(v["initial_quantity"]-v["quantity"] for v in p["parts"].values()),
                "work_hours": sum(sum(v["work_hours"].values()) for v in p["isolations"].values()),
                "current_device_pressures": {key: p["artifacts"][d["test_id"]]["data"]["delivered_pressure"] for key, d in p["devices"].items() if d.get("test_id")},
                "current_supply_revision": p["network"]["revision"],
                "scope": "Measured synthetic pressure and actual physical work; not hydrodynamic realism or model ability"}
    if "contract" in p:
        records = sorted((p["artifacts"][key]["data"] for key in p["settlements"].values()), key=lambda d: d["stage"])
        closeout = [a["data"] for a in p["artifacts"].values() if a["kind"] == "retention_collection" and
                    a["data"]["stage"] == p["contract"]["stages"]+1]
        ages = [d["stage"]-p["artifacts"][key]["data"]["stage"] for d in records for key in d["retention_ids"]
                if p["artifacts"][key]["data"]["retention_cents"] > 0]
        cash, deficits = p["contract"]["opening_cash_cents"], []
        for record in sorted(records, key=lambda d: d["stage"]):
            if cash < record["spend_cents"]:
                deficits.append({"stage": record["stage"], "shortfall_cents": record["spend_cents"]-cash})
            cash += record["reimbursement_cents"]-record["spend_cents"]
        supplier_units = Counter()
        for order in p["orders"].values():
            for row in order["lines"]:
                supplier_units[row["supplier_id"]] += row["received"]
        return {**extra, "profile": p["business_program"], "settled_stages": len(records),
                "positive_applied_retention_age_histogram": dict(Counter(ages)),
                "retention_credits_cents": sum(d["retention_credit_cents"] for d in records)+sum(d["credit_cents"] for d in closeout),
                "closeout_retention_cents": sum(d["credit_cents"] for d in closeout),
                "closing_cash_after_closeout_cents": (records[-1]["closing_cash_cents"] if records else p["contract"]["opening_cash_cents"])+sum(d["credit_cents"] for d in closeout),
                "delivered_supplier_units": dict(supplier_units), "total_spend_cents": sum(d["spend_cents"] for d in records),
                "cancellation_count": sum(a["kind"] == "cancellation" for a in p["artifacts"].values()),
                "refunded_cents": sum(a["data"]["refund_cents"] for a in p["artifacts"].values() if a["kind"] == "cancellation"),
                "matched_schedule_without_retention_liquidity_deficits": deficits,
                "scope": "Applied ledger claims and committed effects; no-retention sensitivity holds the executed supplier schedule fixed, not optimal replanning"}
    return {"profile": None, "scope": "Atomic control workflow; no horizon qualification"}


class OperationsProcessMeasurements:
    def __init__(self):
        self.errors, self.measurement_failures, self.infeasible_checks, self.recoveries = [], [], [], []
        self.first_computations = {}

    def observe(self, before, runtime, record):
        p, output, turn = runtime.project, record["output"], record["turn"]
        name, args = record["name"], record["arguments"]
        run_id = args.get("run_id")
        run = before["active"].get(run_id)
        computation = (name == "execute_step" and args.get("step_id") in {"compute", "submit"}) or (
            name == "run_skill_script" and run and run["skill_id"] != "plan-purchase")
        if computation and run and run_id not in self.first_computations:
            self.first_computations[run_id] = {"run_id": run_id, "skill_id": run["skill_id"], "turn": turn,
                "operation_committed": output.get("ok") is True,
                "measurement_passed": output.get("data", {}).get("passed") if output.get("kind") == "test" else None}
        if not output.get("ok"):
            self.errors.append({"turn": turn, "tool": record["name"], "code": output.get("error", {}).get("code")})
        if output.get("kind") == "test" and output.get("data", {}).get("passed") is False:
            self.measurement_failures.append({"turn": turn, "artifact_id": output["artifact_id"], "device_id": output["data"]["device_id"]})
        if output.get("kind") == "planning_check" and output.get("data", {}).get("feasible") is False:
            self.infeasible_checks.append({"turn": turn, "artifact_id": output["artifact_id"]})
        for notice in p["notices"][len(before["notices"]):]:
            if notice["state_changed"]:
                affected = [key for key, d in p.get("devices", {}).items() if d["status"] == "needs_revalidation"]
                stranded = sorted({key for isolation in p.get("isolations", {}).values()
                    if isolation["open"] and notice["type"] == "supply_pressure_changed"
                    and isolation.get("supply_revision") != p["network"]["revision"]
                    for key in isolation["device_ids"]})
                affected = sorted(set(affected) | set(stranded))
                self.recoveries.append({"event_turn": turn, "type": notice["type"], "affected_devices": affected,
                    "open_isolation_devices_at_update": stranded,
                    "contract_stage": notice.get("stage"), "recovery_measurement_supported": bool(affected or notice.get("stage")),
                    "recovery_turn": None})
        for event in self.recoveries:
            if event["recovery_turn"] is not None:
                continue
            if event["affected_devices"]:
                from .fieldops import _passing_current_test
                recovered = all(p["devices"][key]["status"] == "restored" and _passing_current_test(runtime,
                    runtime.artifact(p["devices"][key]["test_id"])) for key in event["affected_devices"])
                if "services" in p:
                    recovered &= all(p["services"][str(p["devices"][key]["stage"])]["active"] for key in event["affected_devices"])
            elif event["contract_stage"]:
                recovered = str(event["contract_stage"]) in p["settlements"]
            else:
                recovered = False
            if recovered:
                event["recovery_turn"] = turn

    def report(self):
        return {"runtime_errors": self.errors, "failed_measurements": self.measurement_failures,
                "infeasible_planning_checks": self.infeasible_checks, "source_event_recoveries": self.recoveries,
                "first_computation_per_run": list(self.first_computations.values()),
                "first_computation_scope": "Evaluated procedural runs, not business-goal first-pass accuracy; new runs create new units. Macro calls and unfinished runs without computation are not included.",
                "scope": "Offline observed incidents; infeasible drafts may be deliberate exploration. Event recovery is domain-specific, not optimal skill-selection accuracy"}
