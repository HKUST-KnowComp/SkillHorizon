"""Read-only Coupled V2 local goals at the actual first-completion cut."""

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import timedelta

from .build import KEYS
from .declaration_build import LOCAL_GOALS
from .kernel import BusinessError, instant, require


STATE_KINDS = {"order": "orders", "lot": "lots", "unit": "units", "release": "releases",
               "validation": "validations", "calibration": "calibrations", "disposition": "dispositions"}
PUBLIC_KINDS = set(STATE_KINDS) | {"program", "run", "quality_check", "dispatch"}
METHODS = {"prepare-program-revision": "prepared", "calibrate-and-validate-program": "release",
           "schedule-material-bound-run": "scheduled", "execute-and-observe-batch": "observed",
           "disposition-and-recover-lot": "recovered", "reconcile-production-handoff": "handoff"}


def clause(rule, passed):
    return dict(rule=rule, passed=passed)


class CreationEvidence:
    """Index appended native events once across the consumer's one real replay.

    This is not an event reducer or a second business replay. Event contents come
    from the runtime callback after exact replay dispatch, never actor claims.
    """

    def __init__(self):
        self.count = 0
        self.events = defaultdict(list)

    def observe(self, runtime):
        events = runtime.state["events"]
        if len(events) < self.count:
            raise ValueError("Production evidence moved backwards")
        for event in events[self.count:]:
            self.events[(event["type"], event["entity_id"])].append(deepcopy(event))
        self.count = len(events)

    def one(self, kind, identifier):
        rows = self.events.get((kind, identifier), [])
        require(len(rows) == 1, "ACTUAL_UNIQUE_" + kind.upper())
        return rows[0]


class C50Goals:
    def __init__(self, runtime, initial, evidence=None):
        self.runtime, self.initial = runtime, initial
        self.world, self.state, self.public = runtime.world, runtime.state, runtime.public
        self.evidence = evidence if evidence is not None else CreationEvidence()
        if evidence is None:
            self.evidence.observe(runtime)
        self.cache = {}

    def objects(self, kind):
        if kind in STATE_KINDS:
            return self.state[STATE_KINDS[kind]]
        if kind == "dispatch":
            return {self.world["task_id"]: self.state["dispatch"]} if self.state["dispatch"] else {}
        if kind in KEYS:
            return {row[KEYS[kind]]: row for row in self.world["tables"][kind]}
        return {}

    def row(self, kind, identifier):
        rows = self.objects(kind)
        require(identifier in rows, kind.upper() + "_NOT_FOUND")
        row = rows[identifier]
        if kind in KEYS:
            require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def actual(self, kind, identifier, record):
        event = self.evidence.one(kind, identifier)
        require(event["data"] == record, "ACTUAL_EVENT_CONTENT_" + kind.upper())
        return event

    def prepared(self, snapshot):
        p, tool, profile, points, zones = (snapshot[k] for k in ("program", "tool", "profile", "waypoints", "zones"))
        order = self.row("order", p["order_id"])
        require(order == self.initial["production"]["orders"][order["order_id"]], "ORIGINAL_WORK_ORDER")
        require(p["cell_id"] == order["cell_id"] and p["active_tool_id"] == order["tool_id"]
                and p["quality_profile_id"] == order["profile_id"], "PROGRAM_ORDER_EQUIPMENT_PROFILE")
        current, seen = p["program_id"], set()
        while current != order["program_id"]:
            require(current not in seen, "PROGRAM_REVISION_CYCLE")
            seen.add(current)
            record = self.row("program", current)
            event = self.evidence.one("program.revised", current)["data"]
            require(event["program"]["order_id"] == order["order_id"]
                    and event["source_program_id"] == record.get("source_program_id"), "ACTUAL_PROGRAM_REVISION")
            current = event["source_program_id"]
        require(type(p["version"]) is int and p["version"] >= 1 and type(p["cycle_time_sec"]) is int
                and 5 <= p["cycle_time_sec"] <= 60 and type(p["process_offset_units"]) is int
                and -10 <= p["process_offset_units"] <= 10, "PUBLIC_PROGRAM_BOUNDS")
        require(all(r["cell_id"] == p["cell_id"] for r in [tool, profile, *points, *zones]), "SAME_CELL")
        require(tool["tool_id"] == order["tool_id"] and tool["payload_kg"] >= p["payload_kg"] > 0, "TOOL_PAYLOAD")
        require(profile["profile_id"] == order["profile_id"] and profile["status"] == "active"
                and len(profile["metric_definitions"]) == 2
                and {m["metric_name"] for m in profile["metric_definitions"]} == set(self.public["coupled_material_policy"]["metrics"]),
                "MANDATORY_COUPLED_PROFILE")
        require(len(points) >= 2 and [w["waypoint_id"] for w in points] == p["waypoint_ids"]
                and [w["sequence_index"] for w in points] == list(range(1, len(points) + 1))
                and all(w["program_id"] == p["program_id"] for w in points), "ORDERED_WAYPOINTS")
        require(zones and [z["zone_id"] for z in zones] == p["safety_zone_ids"]
                and all(z["enabled"] and z["severity"] in {"protective_stop", "emergency_stop"} for z in zones), "PROTECTIVE_ENVELOPE")
        require(all(1 <= w["speed_mm_s"] <= min(z["max_speed_mm_s"] for z in zones) for w in points), "SPEED_ENVELOPE")
        require(all(set(w["pose"]) == {"x_mm", "y_mm", "z_mm", "rx_deg", "ry_deg", "rz_deg"}
                    and all(type(v) is int and (-500 <= v <= 500 if k.endswith("mm") else -180 <= v <= 180)
                            for k, v in w["pose"].items()) for w in points), "COORDINATE_ENVELOPE")

    def release(self, identifier, current):
        release = self.row("release", identifier)
        self.actual("program.released", identifier, release)
        validation = self.row("validation", release["validation_id"])
        self.actual("program.validated", validation["validation_id"], validation)
        require(validation["program_id"] == release["program_id"] and validation["snapshot"] == release["snapshot"]
                and validation["passed"] is True and validation["checks"]
                and all(v is True for v in validation["checks"].values()), "PASSING_SNAPSHOT_VALIDATION")
        snap = release["snapshot"]
        self.prepared(snap)
        tool = snap["tool"]
        measurement = self.row("calibration", tool["calibration_id"])
        self.actual("calibration.measured", measurement["calibration_id"], measurement)
        require(measurement["source"] == "environment_calibration_test" and tool["calibration_status"] == "valid"
                and (measurement["tool_id"], measurement["tool_revision"]) == (tool["tool_id"], tool["revision"])
                and tool["compensation_units"] == -measurement["observed_bias_units"], "MEASURED_CALIBRATION_AND_COMPENSATION")
        require(snap["program"]["program_id"] == release["program_id"], "RELEASE_PROGRAM_IDENTITY")
        if current:
            # Existing read-only runtime guard, never validate_production_program.
            self.runtime.release(identifier, current=True)
            require(self.row("program", release["program_id"])["release_id"] == identifier, "CURRENT_PUBLISHED_RELEASE")
        return release

    def scheduled(self, identifier):
        run = self.row("run", identifier)
        require(run["run_status"] in {"queued", "running", "completed"}, "RUN_NOT_CANCELLED")
        event = self.evidence.one("run.scheduled", identifier)
        scheduled = event["data"]
        changing = {"run_status", "started_at", "ended_at", "cycle_count", "defect_count", "observed_at",
                    "quality_check_ids", "output_unit_ids", "output_lot_id", "disposition_id", "updated_at",
                    "execution_snapshot", "execution_calibration"}
        require({k: v for k, v in run.items() if k not in changing} ==
                {k: v for k, v in scheduled.items() if k not in changing}, "IMMUTABLE_SCHEDULE_EVIDENCE")
        require(scheduled["run_status"] == "queued", "ACTUAL_QUEUED_PLAN")
        release = self.release(run["release_id"], current=run["run_status"] == "queued")
        order = self.row("order", run["order_id"])
        program = release["snapshot"]["program"]
        require(program["order_id"] == order["order_id"] and program["program_id"] == run["program_id"]
                and all(run[k] == order[k] for k in ("cell_id", "tool_id", "fixture_id")), "RUN_ORDER_EQUIPMENT")
        groups = run["input_groups"]
        flat = [i for group in groups for i in group]
        material = {u["unit_id"]: u for u in run["input_evidence"]}
        require(groups and len(flat) == len(set(flat)) == len(material) and set(flat) == set(material)
                and run["planned_cycle_count"] == len(groups), "EXACT_MATERIAL_GROUPS")
        slots = []
        for index, group in enumerate(groups):
            units = [material[i] for i in group]
            for unit in units:
                actual = self.row("unit", unit["unit_id"])
                require(all(actual[k] == unit[k] for k in ("lot_id", "order_id", "level", "output_slot", "parent_unit_ids", "produced_run_id")),
                        "ORIGINAL_MATERIAL_IDENTITY")
                require(actual["status"] == ("reserved" if run["run_status"] == "queued" else "consumed")
                        and actual["reserved_by"] == (identifier if run["run_status"] == "queued" else None)
                        and actual["consumed_by"] == (None if run["run_status"] == "queued" else identifier), "MATERIAL_RESERVATION_CONSUMPTION")
            if run["rework"]:
                require(len(units) == 1 and units[0]["order_id"] == order["order_id"]
                        and units[0]["status"] == "quarantined" and units[0]["quality_accepted"] is False, "EXPLICIT_REWORK_INPUT")
                slots.append(units[0]["output_slot"])
            else:
                require(all(u["status"] == "available" and u["quality_accepted"] is True for u in units), "ACCEPTED_INPUT_MATERIAL")
                if order["level"]:
                    require(Counter(u["order_id"] for u in units) == {c["parent_order_id"]: c["units_per_output"] for c in order["bom"]}
                            and all(u["level"] == self.state["orders"][u["order_id"]]["level"] < order["level"] for u in units), "EXACT_BOM")
                else:
                    require(len(units) == 1 and units[0]["lot_id"] in order["raw_lot_ids"] and units[0]["level"] == -1, "ORIGINAL_RAW_LOT")
                slots.append(index)
        require(slots == run["output_slots"] and len(slots) == len(set(slots))
                and (run["rework"] or len(slots) == order["output_quantity"]), "EXACT_OUTPUT_SLOTS")
        attempts = [r for r in self.objects("run").values() if r["order_id"] == order["order_id"] and r.get("stop_reason") != "cancelled_before_start"]
        require(len(attempts) <= order["max_attempts"] and (run["rework"] or
                sum(not r["rework"] for r in attempts) == 1), "BOUNDED_ATTEMPTS")
        policy = self.public["coupled_material_policy"]
        pressure, shim = run["pressure_units"], run["shim_units"]
        require(len(pressure) == len(shim) == len(groups) and all(type(v) is int and -10 <= v <= 10 for v in pressure + shim), "COUPLED_CONTROL_COVERAGE")
        setup = sum(abs(p) * policy["pressure_seconds_per_unit"] + abs(s) * policy["shim_seconds_per_unit"] for p, s in zip(pressure, shim))
        start, end = instant(run["scheduled_start"]), instant(run["scheduled_end"])
        require(run["setup_seconds"] == setup and end - start == timedelta(seconds=program["cycle_time_sec"] * len(groups) + setup)
                and start >= instant(event["occurred_at"])
                and end <= instant(self.public["origin"]) + timedelta(seconds=self.public["policy"]["maximum_clock_seconds"]), "SETUP_INTERVAL_HORIZON")
        for reservation in self.public.get("equipment_calendar", []):
            if any(run[k] == reservation[k] for k in ("cell_id", "tool_id", "fixture_id")):
                require(end <= instant(reservation["scheduled_start"]) or start >= instant(reservation["scheduled_end"]), "MAINTENANCE_INTERVAL")
        if run["run_status"] == "queued":
            require(start >= instant(self.world["clock"]), "QUEUED_START_NOT_PAST")
            for other in self.objects("run").values():
                if other["run_id"] != identifier and other["run_status"] in {"queued", "running"} and any(run[k] == other[k] for k in ("cell_id", "tool_id", "fixture_id")):
                    require(end <= instant(other["scheduled_start"]) or start >= instant(other["scheduled_end"]), "SHARED_RESOURCE_INTERVAL")
        else:
            started = self.evidence.one("run.started", identifier)["data"]
            require(run["execution_snapshot"] == started["execution_snapshot"] == release["snapshot"]
                    and run["execution_calibration"] == started["execution_calibration"] ==
                    self.row("calibration", release["snapshot"]["tool"]["calibration_id"])
                    and run["started_at"] == started["started_at"] == run["scheduled_start"], "ACTUAL_EXECUTION_SNAPSHOT")
        return run

    def observed(self, identifier):
        run = self.scheduled(identifier)
        require(run["observed_at"] is not None and instant(run["observed_at"]) >= instant(run["scheduled_end"])
                and instant(self.world["clock"]) >= instant(run["observed_at"]), "PROCESSING_AND_OBSERVATION_COMPLETE")
        observed = self.evidence.one("run.observed", identifier)["data"]
        fields = ("run_id", "release_id", "execution_snapshot", "execution_calibration", "observed_at",
                  "quality_check_ids", "output_unit_ids", "output_lot_id", "input_groups", "output_slots")
        require(all(run[k] == observed["run"][k] for k in fields), "ACTUAL_OBSERVED_RUN")
        uids, qids = run["output_unit_ids"], run["quality_check_ids"]
        metrics = run["execution_snapshot"]["profile"]["metric_definitions"]
        require(len(uids) == len(set(uids)) == len(run["input_groups"])
                and len(qids) == len(set(qids)) == len(uids) * len(metrics), "ALL_UNIT_METRIC_OBSERVATIONS")
        checks = [self.row("quality_check", i) for i in qids]
        require(checks == observed["checks"], "REAL_ENVIRONMENT_MEASUREMENTS")
        lot = self.row("lot", run["output_lot_id"])
        require(lot["unit_ids"] == uids and lot["produced_run_id"] == identifier and lot["order_id"] == run["order_id"], "OUTPUT_LOT_IDENTITY")
        accepted, failed = [], []
        for index, (uid, group, slot) in enumerate(zip(uids, run["input_groups"], run["output_slots"])):
            unit = self.row("unit", uid)
            require(all(unit[k] == observed["units"][index][k] for k in
                        ("unit_id", "lot_id", "order_id", "level", "output_slot", "parent_unit_ids", "produced_run_id"))
                    and unit["produced_run_id"] == identifier and unit["output_slot"] == slot
                    and unit["parent_unit_ids"] == group, "ACTUAL_OUTPUT_UNIT_LINEAGE")
            rows = [c for c in checks if c["unit_id"] == uid]
            require(Counter(c["metric_name"] for c in rows) == Counter(m["metric_name"] for m in metrics), "MANDATORY_METRIC_SET")
            for check in rows:
                metric = next(m for m in metrics if m["metric_name"] == check["metric_name"])
                require(check["run_id"] == identifier and check["release_id"] == run["release_id"]
                        and check["cell_id"] == run["cell_id"] and check["profile_id"] == run["execution_snapshot"]["profile"]["profile_id"]
                        and check["profile_revision"] == run["execution_snapshot"]["profile"]["revision"]
                        and check["output_slot"] == slot and check["parent_unit_ids"] == group
                        and check["source"] == "environment_discrete_process" and check["inspector"] == "environment"
                        and all(check[k] == metric[k] for k in ("unit", "expected_min", "expected_max")), "MEASUREMENT_BINDING")
                require(check["result"] == ("pass" if metric["expected_min"] <= check["measured_value"] <= metric["expected_max"] else "fail"), "MEASUREMENT_RESULT")
            (accepted if all(c["result"] == "pass" for c in rows) else failed).append(uid)
        return run, accepted, failed

    def partition(self, identifier):
        run, accepted, failed = self.observed(identifier)
        disposition = self.row("disposition", run["disposition_id"])
        self.actual("run.disposition", disposition["disposition_id"], disposition)
        require(run["run_status"] == "completed" and disposition["accepted_unit_ids"] == accepted
                and disposition["quarantined_unit_ids"] == failed and disposition["run_id"] == identifier
                and disposition["lot_id"] == run["output_lot_id"] and disposition["release_id"] == run["release_id"]
                and disposition["order_id"] == run["order_id"] and disposition["quality_check_ids"] == run["quality_check_ids"]
                and disposition["produced_quantity"] == run["cycle_count"] == len(accepted) + len(failed)
                and disposition["defective_unit_count"] == run["defect_count"] == len(failed)
                and disposition["completed_at"] == run["ended_at"], "ACTUAL_QC_PARTITION")
        for uid in accepted:
            unit = self.row("unit", uid)
            require(unit["quality_accepted"] is True and unit["status"] in {"available", "reserved", "consumed", "dispatched"}, "ACCEPTED_SIBLING_PRESERVED")
            if unit["status"] in {"reserved", "consumed"}:
                consumer = self.row("run", unit["reserved_by"] if unit["status"] == "reserved" else unit["consumed_by"])
                evidence = self.evidence.one("run.scheduled" if unit["status"] == "reserved" else "run.started", consumer["run_id"])["data"]
                require(consumer["input_groups"] == evidence["input_groups"]
                        and sum(uid in g for g in consumer["input_groups"]) == 1, "ACCEPTED_SIBLING_ACTUAL_CONSUMPTION")
        for uid in failed:
            require(self.row("unit", uid)["quality_accepted"] is False, "FAILED_HISTORY_PRESERVED")
        return run, accepted, failed

    def recovered(self, identifier):
        pending, seen = [identifier], set()
        while pending:
            current = pending.pop()
            require(current not in seen, "REWORK_LINEAGE_CYCLE")
            seen.add(current)
            run, _, failed = self.partition(current)
            for uid in failed:
                unit = self.row("unit", uid)
                require(unit["status"] == "consumed" and unit["consumed_by"] is not None and unit["reserved_by"] is None,
                        "QUARANTINED_INPUT_NOT_RECOVERED")
                child = self.row("run", unit["consumed_by"])
                require(child["rework"] is True and child["order_id"] == run["order_id"]
                        and child["started_at"] is not None and instant(child["started_at"]) >= instant(run["ended_at"])
                        and instant(child["scheduled_end"]) > instant(run["ended_at"]), "REAL_REWORK_ATTEMPT")
                positions = [i for i, group in enumerate(child["input_groups"]) if group == [uid]]
                require(len(positions) == 1 and child["output_slots"][positions[0]] == unit["output_slot"], "REPLACEMENT_SLOT_LINEAGE")
                # A single bounded repair batch may recover several failed siblings.
                if child["run_id"] not in seen and child["run_id"] not in pending:
                    pending.append(child["run_id"])
                else:
                    require(child["run_id"] != current, "REWORK_LINEAGE_CYCLE")
        return True

    def handoff(self):
        dispatch = self.row("dispatch", self.world["task_id"])
        self.actual("line.dispatched", self.world["task_id"], dispatch)
        units = [self.row("unit", i) for i in dispatch["unit_ids"]]
        require(len(units) == len(set(dispatch["unit_ids"])) and all(u["status"] == "dispatched" and u["quality_accepted"] is True
                and u["order_id"] in self.public["terminal_order_ids"] for u in units), "FROZEN_TERMINAL_UNITS")
        for key in self.public["terminal_order_ids"]:
            order = self.row("order", key)
            selected = [u for u in units if u["order_id"] == key]
            require(len(selected) == order["output_quantity"] and {u["output_slot"] for u in selected} == set(range(order["output_quantity"]))
                    and dispatch["terminal_quantities"][key] == order["output_quantity"], "EXACT_TERMINAL_SLOT_JOIN")
        producers = {u["produced_run_id"] for u in units}
        for identifier in producers:
            self.partition(identifier)
        require(set(dispatch["disposition_ids"]) == {self.row("run", i)["disposition_id"] for i in producers}, "TERMINAL_DISPOSITION_JOIN")
        require(not any(r["run_status"] in {"queued", "running"} for r in self.objects("run").values()), "NO_ACTIVE_RUNS")
        require(len(self.state["applied_updates"]) == len(self.state["scheduled_updates"]), "DECLARED_UPDATES_COMPLETE")
        require(all(u["unit_id"] in dispatch["unit_ids"] or u["status"] == "consumed" for u in self.state["units"].values()), "MATERIAL_CONSERVATION")
        self.runtime.validate_state()

    def check(self, method, identifier):
        key = method, identifier
        if key not in self.cache:
            try:
                if method == "prepared":
                    self.prepared(self.runtime.snapshot(identifier))
                elif method == "release":
                    self.release(identifier, current=True)
                elif method == "handoff":
                    self.handoff()
                else:
                    getattr(self, method)(identifier)
            except BusinessError as error:
                self.cache[key] = clause(str(error), False)
            else:
                self.cache[key] = clause("VERIFIED_" + method.upper(), True)
        return deepcopy(self.cache[key])

    def resolve(self, refs, clauses):
        result = []
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                    or any(not isinstance(ref[k], str) or not ref[k] for k in ref)):
                raise ValueError("Production references require kind/id strings")
            kind, key = ref["kind"], ref["id"]
            valid = kind in PUBLIC_KINDS and key in self.objects(kind)
            clauses.append(clause("EXPLICIT_REFERENCE_EXISTS", valid))
            if valid:
                try:
                    row = self.row(kind, key)
                except BusinessError as error:
                    clauses.append(clause(str(error), False))
                else:
                    result.append((kind, key, row))
        return result

    def repair_descendant(self, identifier, roots):
        pending, seen = [identifier], set()
        while pending:
            key = pending.pop()
            if key in roots:
                return True
            if key in seen:
                continue
            seen.add(key)
            run = self.row("run", key)
            if run["rework"]:
                pending.extend(self.row("unit", i)["produced_run_id"] for g in run["input_groups"] for i in g
                               if self.row("unit", i)["produced_run_id"] is not None)
        return False

    def target(self, kind, key, row, method):
        if method == "prepared":
            return key if kind == "program" else None
        if method == "release":
            if kind == "release":
                return key
            if kind == "program":
                return row["release_id"]
            if kind == "validation":
                try:
                    self.actual("program.validated", key, row)
                    require(row["passed"] is True and row["checks"] and all(v is True for v in row["checks"].values())
                            and row["snapshot"]["program"]["program_id"] == row["program_id"], "EXPLICIT_PASSING_VALIDATION")
                    program = self.row("program", row["program_id"])
                    release = self.runtime.release(program["release_id"], current=True)
                    require(release["program_id"] == row["program_id"] and release["snapshot"] == row["snapshot"],
                            "EXPLICIT_VALIDATION_RELEASE_SNAPSHOT")
                    return release["release_id"]
                except BusinessError:
                    return None
            return None
        if method == "handoff":
            return self.world["task_id"] if kind == "dispatch" else None
        if kind == "run":
            return key
        if kind in {"lot", "unit"}:
            return row["produced_run_id"]
        if kind in {"quality_check", "disposition"}:
            return row["run_id"]
        return None

    def assess(self, claim):
        skill = claim["skill_id"]
        checks = []
        scope = self.resolve(claim.get("scope", []), checks)
        outputs = self.resolve(claim.get("outputs", []), checks)
        if skill not in LOCAL_GOALS:
            raise ValueError("Unknown C50 skill")
        if skill == "inspect-production-order":
            checks.extend(clause("COGNITIVE_ORDER_SCOPE", kind == "order") for kind, _, _ in scope + outputs)
            checks.append(clause("UNDERSTANDING_UNOBSERVED", None))
        else:
            method = METHODS[skill]
            allowed = {"prepared": {"program", "order"}, "release": {"program", "release", "validation", "calibration", "order"},
                       "scheduled": {"run", "order"}, "observed": {"run", "lot", "unit", "quality_check", "order"},
                       "recovered": {"run", "lot", "unit", "disposition", "order"},
                       "handoff": {"dispatch", "unit", "disposition", "order"}}[method]
            checks.extend(clause("REFERENCE_KIND_FOR_GOAL", kind in allowed) for kind, _, _ in scope + outputs)
            refs = [r for r in scope + outputs if r[0] in allowed]
            if method == "handoff":
                dispatch = self.state["dispatch"]
                for kind, key, row in outputs:
                    if kind in {"unit", "disposition"}:
                        checks.append(clause("EXPLICIT_TERMINAL_HANDOFF_OUTPUT",
                                             row.get("order_id") in self.public["terminal_order_ids"]))
                        if kind == "unit":
                            checks.append(clause("EXPLICIT_PRODUCED_ACCEPTED_UNIT", row.get("produced_run_id") is not None
                                                 and row.get("output_slot") is not None and row.get("quality_accepted") is True))
                        if dispatch is not None:
                            checks.append(clause("OUTPUT_HANDOFF_BINDING", key in dispatch.get(
                                "unit_ids" if kind == "unit" else "disposition_ids", [])))
            for ref in refs:
                if ref[0] not in {"order", "calibration"} and method != "handoff":
                    target = self.target(*ref, method)
                    checks.append(clause("REFERENCE_HAS_GOAL_TARGET", target is not None))
                    if target is not None and method in {"scheduled", "observed", "recovered"}:
                        run = self.row("run", target)
                        kind, key, _ = ref
                        if kind in {"unit", "quality_check"}:
                            checks.append(clause("EXPLICIT_RUN_OUTPUT_MEMBERSHIP", key in run[
                                "output_unit_ids" if kind == "unit" else "quality_check_ids"]))
                        if kind in {"lot", "disposition"}:
                            checks.append(clause("EXPLICIT_RUN_ARTIFACT_BINDING", key == run[
                                "output_lot_id" if kind == "lot" else "disposition_id"]))
            targets = list(dict.fromkeys(t for r in refs if (t := self.target(*r, method)) is not None))
            if not targets:
                # An explicit program without a release is a known failure, not ambiguity.
                missing_release = method == "release" and any(r[0] in {"program", "validation"} for r in refs)
                checks.append(clause("LOCAL_TARGET_RESOLVED", False if missing_release else None))
            else:
                target_orders = set()
                for key in targets:
                    if method == "handoff":
                        target_orders.update(self.public["terminal_order_ids"])
                    else:
                        row = self.row("program" if method == "prepared" else "release" if method == "release" else "run", key)
                        target_orders.add(row["snapshot"]["program"]["order_id"] if method == "release" else row["order_id"])
                    checks.append(self.check(method, key))
                orders = {key for kind, key, _ in scope if kind == "order"}
                if orders:
                    checks.append(clause("SCOPE_ORDER_COVERAGE", True if orders == target_orders else
                                         None if target_orders.issubset(orders) else False))
                anchors = {t for r in scope if (t := self.target(*r, method)) is not None}
                for kind, key, row in refs:
                    target = self.target(kind, key, row, method)
                    if anchors and (kind, key, row) in outputs and target is not None:
                        checks.append(clause("OUTPUT_TARGET", target in anchors or
                                             (method == "recovered" and self.repair_descendant(target, anchors))))
                    if kind == "calibration" and method == "release":
                        checks.append(clause("OUTPUT_CALIBRATION_BINDING", any(self.row("release", t)["snapshot"]["tool"]["calibration_id"] == key for t in targets)))
                    if kind == "order" and (kind, key, row) in outputs:
                        checks.append(clause("OUTPUT_ORDER_BINDING", key in target_orders))
                    if method == "handoff" and kind in {"unit", "disposition"}:
                        dispatch = self.state["dispatch"] or {}
                        checks.append(clause("OUTPUT_HANDOFF_BINDING", key in dispatch.get("unit_ids" if kind == "unit" else "disposition_ids", [])))
        verdict = "contradicted" if any(c["passed"] is False for c in checks) else "unknown" if not checks or any(c["passed"] is None for c in checks) else "supported"
        return {**{k: claim[k] for k in ("run_id", "skill_id", "event_index", "last_resolved_business_dispatch")},
                "scope": deepcopy(claim.get("scope", [])), "outputs": deepcopy(claim.get("outputs", [])),
                "goal_verdict": verdict, "clauses": checks, "process_adherence": "unobserved",
                "reason": "Actual local production evidence; understanding and process adherence are unobserved"}
