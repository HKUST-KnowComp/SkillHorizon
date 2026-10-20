"""Independent manufacturing replay from frozen obligations and actual evidence.

No runtime/checker/actor-script imports. A completed run is not an acceptance criterion.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path

from integrations.internal_mcp.io import read_json


PRIMARY = {"robot_cell": "cell_id", "program": "program_id", "waypoint": "waypoint_id", "robot_tool": "tool_id", "fixture": "fixture_id",
    "safety_zone": "zone_id", "safety_event": "event_id", "quality_check_profile": "profile_id", "quality_check": "check_id", "run": "run_id", "audit_log": "audit_id"}


def date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone absent")
    return result.astimezone(timezone.utc)


def evaluate(task, runtime):
    task, runtime = Path(task), Path(runtime)
    spec = read_json(task / "private/initial_obligations.json")
    world = read_json(runtime / "world.json")
    public, initial, state = spec["public"], spec["initial_state"], world["production"]
    ref = deepcopy(initial)
    tables = {kind: {row[PRIMARY[kind]]: deepcopy(row) for row in rows} for kind, rows in spec["initial_tables"].items()}
    errors = []

    def check(condition, code):
        if not condition:
            errors.append(code)

    def edit(kind, identifier, patch, now):
        tables[kind][identifier].update(deepcopy({**patch, "updated_at": now}))

    def snapshot(identifier):
        program = tables["program"][identifier]
        return {"program": {k: deepcopy(program[k]) for k in ("program_id", "order_id", "cell_id", "version", "cycle_time_sec", "payload_kg", "process_offset_units", "active_tool_id", "quality_profile_id", "waypoint_ids", "safety_zone_ids")},
            "waypoints": [{k: deepcopy(w[k]) for k in ("waypoint_id", "program_id", "cell_id", "sequence_index", "pose", "speed_mm_s", "blend_radius_mm", "approach")}
                for w in [tables["waypoint"][i] for i in program["waypoint_ids"]]],
            "tool": {k: deepcopy(tables["robot_tool"][program["active_tool_id"]][k]) for k in ("tool_id", "cell_id", "revision", "payload_kg", "calibration_status", "calibration_id", "compensation_units")},
            "zones": [{k: deepcopy(z[k]) for k in ("zone_id", "cell_id", "revision", "enabled", "severity", "max_speed_mm_s", "polygon_points")}
                for z in [tables["safety_zone"][i] for i in program["safety_zone_ids"]]],
            "profile": deepcopy(tables["quality_check_profile"][program["quality_profile_id"]])}

    def occupied(cell_id):
        return any(r["cell_id"] == cell_id and r["run_status"] == "running" for r in tables["run"].values())

    def valid_release(identifier):
        release = ref["releases"][identifier]
        check(release["snapshot"] == snapshot(release["program_id"]) and tables["program"][release["program_id"]]["status"] == "released", "STALE_EXECUTION_RELEASE")
        return release

    check(world["task_id"] == public["task_id"] and read_json(task / "public/task.json") == public, "TASK_OR_PUBLIC_POLICY_CHANGED")
    for key in ("orders", "response_profiles", "scheduled_updates"):
        check(state[key] == initial[key], "IMMUTABLE_BUSINESS_INPUT_CHANGED")
    check(state.get("first_pass_deviations", {}) == initial.get("first_pass_deviations", {}), "IMMUTABLE_FIRST_PASS_VARIATION")
    now, previous_sequence, event_ids = public["origin"], -1, set()
    bias_at_start = {}
    for event in state["events"]:
        try:
            check(event["event_id"] not in event_ids and event["sequence"] >= previous_sequence and date(event["occurred_at"]) >= date(now), "EVENT_IDENTITY_ORDER")
            event_ids.add(event["event_id"])
            previous_sequence, now = event["sequence"], event["occurred_at"]
            check(date(now) <= date(public["origin"]) + timedelta(seconds=public["policy"]["maximum_clock_seconds"]), "EVENT_OUTSIDE_TIME_HORIZON")
            kind, identifier, data = event["type"], event["entity_id"], event["data"]
            check(event["cell_id"] in public["cell_ids"], "EVENT_CELL_IDENTITY")
            if kind == "program.edited":
                before, after = tables["program"][identifier], data["after"]
                check(data["before"] == before and before["status"] in {"draft", "validated"}, "PROGRAM_EDIT_PRECONDITION")
                expected = {**before, "cycle_time_sec": after["cycle_time_sec"], "process_offset_units": after["process_offset_units"],
                    "version": before["version"] + 1, "status": "draft", "validation_id": None, "updated_at": now}
                check(after == expected and type(after["cycle_time_sec"]) is int and 5 <= after["cycle_time_sec"] <= 60 and
                    type(after["process_offset_units"]) is int and -10 <= after["process_offset_units"] <= 10, "PROGRAM_CONTENT_OR_REVISION")
                tables["program"][identifier] = deepcopy(expected)
            elif kind == "waypoint.edited":
                before, after = tables["waypoint"][identifier], data["after"]
                parent = tables["program"][before["program_id"]]
                check(data["before"] == before and not before["locked"] and parent["status"] in {"draft", "validated"}, "WAYPOINT_EDIT_PRECONDITION")
                expected = {**before, "pose": deepcopy(after["pose"]), "speed_mm_s": after["speed_mm_s"], "updated_at": now}
                check(after == expected and type(after["speed_mm_s"]) is int and 1 <= after["speed_mm_s"] <= 1000, "WAYPOINT_CONTENT_IDENTITY")
                check(set(after["pose"]) == {"x_mm", "y_mm", "z_mm", "rx_deg", "ry_deg", "rz_deg"} and
                    all(type(v) is int and abs(v) <= (500 if k.endswith("mm") else 180) for k, v in after["pose"].items()), "WAYPOINT_COORDINATES")
                tables["waypoint"][identifier] = deepcopy(expected)
                edit("program", parent["program_id"], {"version": parent["version"] + 1, "status": "draft", "validation_id": None}, now)
                check(data["program"] == parent, "WAYPOINT_PARENT_REVISION")
            elif kind == "program.revised":
                source, program = tables["program"][data["source_program_id"]], data["program"]
                check(identifier not in tables["program"] and program["program_id"] == identifier and
                    not any(p["name"] == program["name"] and p["cell_id"] == source["cell_id"] for p in tables["program"].values()), "NEW_PROGRAM_IDENTITY")
                check(len(data["waypoints"]) == len(source["waypoint_ids"]) and len({w["waypoint_id"] for w in data["waypoints"]}) == len(data["waypoints"]), "REVISION_WAYPOINT_SET")
                copied = []
                for waypoint, source_id in zip(data["waypoints"], source["waypoint_ids"]):
                    wid = waypoint["waypoint_id"]
                    expected = {**tables["waypoint"][source_id], "waypoint_id": wid, "program_id": identifier, "locked": False, "created_at": now, "updated_at": now}
                    check(wid not in tables["waypoint"] and waypoint == expected, "REVISION_WAYPOINT_CONTENT")
                    tables["waypoint"][wid] = deepcopy(expected)
                    copied.append(wid)
                expected = {**source, "program_id": identifier, "name": program["name"], "version": source["version"] + 1, "source_program_id": source["program_id"],
                    "status": "draft", "waypoint_ids": copied, "validation_id": None, "release_id": None, "created_at": now, "updated_at": now}
                check(program == expected, "REVISION_PROGRAM_CONTENT")
                tables["program"][identifier] = deepcopy(expected)
                tool = tables["robot_tool"][program["active_tool_id"]]
                edit("robot_tool", tool["tool_id"], {"compatible_program_ids": tool["compatible_program_ids"] + [identifier]}, now)
                for zid in program["safety_zone_ids"]:
                    edit("safety_zone", zid, {"linked_program_ids": tables["safety_zone"][zid]["linked_program_ids"] + [identifier]}, now)
            elif kind == "calibration.measured":
                tool = tables["robot_tool"][data["tool_id"]]
                check(not occupied(tool["cell_id"]) and identifier not in ref["calibrations"], "CALIBRATION_TEST_PRECONDITION")
                expected = {"calibration_id": identifier, "tool_id": tool["tool_id"], "tool_revision": tool["revision"],
                    "observed_bias_units": ref["tool_biases"][tool["tool_id"]], "measured_at": now, "source": "environment_calibration_test"}
                check(data == expected, "ENVIRONMENT_CALIBRATION_MEASUREMENT")
                ref["calibrations"][identifier] = expected
            elif kind == "calibration.applied":
                tool, after = tables["robot_tool"][identifier], data["after"]
                measurement = ref["calibrations"][after["calibration_id"]]
                check(data["before"] == tool and not occupied(tool["cell_id"]), "CALIBRATION_APPLY_PRECONDITION")
                check(measurement["tool_id"] == identifier and measurement["tool_revision"] == tool["revision"], "CALIBRATION_TOOL_REVISION")
                edit("robot_tool", identifier, {"calibration_status": "valid", "calibration_id": measurement["calibration_id"],
                    "compensation_units": -measurement["observed_bias_units"], "last_calibrated_at": now}, now)
                check(after == tool, "CALIBRATION_COMPENSATION_OR_IDENTITY")
            elif kind == "tool.drifted":
                planned = initial["scheduled_updates"][0]
                tool = tables["robot_tool"][identifier]
                roots = [o for o in ref["orders"].values() if o["level"] <= (planned['after_level'] if public.get('instance_revision') == 'factoryops_portfolio150_v1' else 0)]
                check(not ref["applied_updates"] and not occupied(tool["cell_id"]) and identifier == planned["tool_id"], "TOOL_DRIFT_PRECONDITION")
                check(all(sum(u["order_id"] == o["order_id"] and u["quality_accepted"] for u in ref["units"].values()) == o["output_quantity"] for o in roots), "TOOL_DRIFT_LAYER_BOUNDARY")
                check(data["before"] == tool, "TOOL_DRIFT_BEFORE_VERSION")
                edit("robot_tool", identifier, {"revision": tool["revision"] + 1, "calibration_status": "due", "calibration_id": None, "compensation_units": 0}, now)
                ref["tool_biases"][identifier] = planned["new_bias_units"]
                expected = {**{k: v for k, v in planned.items() if k != "new_bias_units"}, "tool_revision": tool["revision"], "released_at": now}
                check(data["after"] == tool and data["update"] == expected, "TOOL_DRIFT_EFFECT")
                ref["applied_updates"].append(expected)
            elif kind == "program.validated":
                current = snapshot(data["program_id"])
                program, tool, profile, points, zones = (current[k] for k in ("program", "tool", "profile", "waypoints", "zones"))
                cell = tables["robot_cell"][program["cell_id"]]
                calibration = ref["calibrations"].get(tool["calibration_id"])
                checks = {"cell_available": cell["status"] == "active" and cell["network_state"] == "online",
                    "tool_current_calibration": tool["calibration_status"] == "valid" and calibration is not None and calibration["tool_id"] == tool["tool_id"] and calibration["tool_revision"] == tool["revision"] and tool["compensation_units"] == -calibration["observed_bias_units"],
                    "same_cell": all(r["cell_id"] == program["cell_id"] for r in [tool, profile] + points + zones),
                    "tool_payload": tool["payload_kg"] >= program["payload_kg"], "mandatory_profile": profile["status"] == "active" and len(profile["metric_definitions"]) == 2,
                    "ordered_waypoints": len(points) >= 2 and [p["sequence_index"] for p in points] == list(range(1, len(points) + 1)),
                    "protective_envelope": bool(zones) and all(z["enabled"] and z["severity"] in {"protective_stop", "emergency_stop"} for z in zones),
                    "speed_within_envelope": bool(zones) and all(p["speed_mm_s"] <= min(z["max_speed_mm_s"] for z in zones) for p in points),
                    "coordinate_envelope": all(-500 <= p["pose"][k] <= 500 for p in points for k in ("x_mm", "y_mm", "z_mm"))}
                expected = {"validation_id": identifier, "program_id": program["program_id"], "snapshot": current, "checks": checks, "passed": all(checks.values()), "validated_at": now}
                check(identifier not in ref["validations"] and data == expected, "EXACT_PROGRAM_VALIDATION")
                ref["validations"][identifier] = expected
                if tables["program"][program["program_id"]]["status"] != "released":
                    edit("program", program["program_id"], {"status": "validated" if expected["passed"] else "draft", "validation_id": identifier}, now)
            elif kind == "program.released":
                program = tables["program"][data["program_id"]]
                validation = ref["validations"][data["validation_id"]]
                check(identifier not in ref["releases"] and program["status"] == "validated" and validation["passed"] and validation["snapshot"] == snapshot(program["program_id"]), "RELEASE_VALIDATION_PRECONDITION")
                expected = {"release_id": identifier, "program_id": program["program_id"], "validation_id": validation["validation_id"], "snapshot": deepcopy(validation["snapshot"]), "released_at": now}
                check(data == expected, "RELEASE_EXACT_CONTENT")
                ref["releases"][identifier] = expected
                edit("program", program["program_id"], {"status": "released", "release_id": identifier}, now)
                for wid in program["waypoint_ids"]:
                    edit("waypoint", wid, {"locked": True}, now)
            elif kind == "run.scheduled":
                order, release = ref["orders"][data["order_id"]], valid_release(data["release_id"])
                program = release["snapshot"]["program"]
                check(identifier not in tables["run"] and data["run_id"] == identifier and data["task_id"] == public["task_id"], "NEW_RUN_IDENTITY")
                check(program["order_id"] == order["order_id"] and data["program_id"] == program["program_id"] and all(data[k] == order[k] for k in ("cell_id", "tool_id", "fixture_id")), "RUN_EQUIPMENT_ORDER_IDENTITY")
                groups, flat = data["input_groups"], [i for g in data["input_groups"] for i in g]
                check(len(flat) == len(set(flat)), "DUPLICATE_MATERIAL_CONSUMPTION")
                check(data["planned_cycle_count"] == len(groups) and bool(groups) and (data["rework"] or len(groups) == order["output_quantity"]), "ACTUAL_PLANNED_QUANTITY")
                attempts = [r for r in tables["run"].values() if r["order_id"] == order["order_id"] and r.get("stop_reason") != "cancelled_before_start"]
                check((order["max_attempts"] is None or len(attempts) < order["max_attempts"]) and (data["rework"] or not attempts), "EXPLICIT_BOUNDED_REWORK")
                slots = []
                for index, group in enumerate(groups):
                    material = [ref["units"][i] for i in group]
                    if data["rework"]:
                        check(len(material) == 1 and material[0]["order_id"] == order["order_id"] and material[0]["status"] == "quarantined" and not material[0]["quality_accepted"], "REWORK_INPUT_LINEAGE")
                        slots.append(material[0]["output_slot"])
                    else:
                        check(all(u["status"] == "available" and u["quality_accepted"] for u in material), "PARENT_MATERIAL_NOT_AVAILABLE")
                        if order["level"]:
                            if "bom" in order:
                                counts = {parent: sum(u["order_id"] == parent for u in material) for parent in {u["order_id"] for u in material}}
                                check(counts == {c["parent_order_id"]: c["units_per_output"] for c in order["bom"]} and
                                    all(u["level"] == ref["orders"][u["order_id"]]["level"] < order["level"] for u in material), "EXACT_MULTI_AGE_BOM_QUANTITIES")
                            else:
                                check(len(material) == 2 and {u["order_id"] for u in material} == set(order["parent_order_ids"]) and all(u["level"] == order["level"] - 1 for u in material), "EXACT_BOM_PARENT_JOIN")
                        else:
                            check(len(material) == 1 and material[0]["lot_id"] in order["raw_lot_ids"] and material[0]["level"] == -1, "RAW_LOT_IDENTITY")
                        slots.append(index)
                check(data["output_slots"] == slots and len(slots) == len(set(slots)) and data["input_evidence"] == [ref["units"][i] for i in flat], "RESERVATION_MATERIAL_EVIDENCE")
                setup = 0
                if "coupled_material_policy" in public:
                    policy = public["coupled_material_policy"]
                    check(all(len(data[k]) == len(groups) and all(type(v) is int and -10 <= v <= 10 for v in data[k]) for k in ("pressure_units", "shim_units")), "COUPLED_CONTROL_DOMAIN")
                    setup = sum(abs(v) for v in data["pressure_units"]) * policy["pressure_seconds_per_unit"] + sum(abs(v) for v in data["shim_units"]) * policy["shim_seconds_per_unit"]
                    check(data["setup_seconds"] == setup, "ACTUAL_COUPLED_SETUP_TIME")
                elif "material_tolerance_policy" in public:
                    check(len(data["material_offset_units"]) == len(groups) and all(type(v) is int and -10 <= v <= 10 for v in data["material_offset_units"]), "PHYSICAL_MATERIAL_OFFSET_DOMAIN")
                start, end = date(data["scheduled_start"]), date(data["scheduled_end"])
                check(start >= date(now) and end - start == timedelta(seconds=program["cycle_time_sec"] * len(groups) + setup) and end <= date(public["origin"]) + timedelta(seconds=public["policy"]["maximum_clock_seconds"]), "RESERVATION_DURATION_OR_HORIZON")
                for reservation in public.get("equipment_calendar", []):
                    if any(reservation[k] == data[k] for k in ("cell_id", "tool_id", "fixture_id")):
                        check(end <= date(reservation["scheduled_start"]) or start >= date(reservation["scheduled_end"]), "EQUIPMENT_MAINTENANCE_COLLISION")
                for prior in tables["run"].values():
                    if prior["run_status"] in {"queued", "running"} and any(prior[k] == data[k] for k in ("cell_id", "tool_id", "fixture_id")):
                        check(end <= date(prior["scheduled_start"]) or start >= date(prior["scheduled_end"]), "EQUIPMENT_INTERVAL_COLLISION")
                check(data["run_status"] == "queued" and data["cycle_count"] == data["defect_count"] == 0 and not data["quality_check_ids"] and not data["output_unit_ids"] and
                    all(data[k] is None for k in ("started_at", "ended_at", "observed_at", "output_lot_id", "disposition_id", "stop_reason")), "UNSTARTED_RUN_STATE")
                check(data["operator_name"] == public["actor"] and data["created_at"] == data["updated_at"] == now, "RUN_AUTHORITY_AND_TIME")
                tables["run"][identifier] = deepcopy(data)
                for uid in flat:
                    ref["units"][uid].update(status="reserved", reserved_by=identifier)
            elif kind == "run.cancelled":
                run = tables["run"][identifier]
                check(run["run_status"] == "queued", "CANCELLATION_CANNOT_ERASE_EXECUTION")
                for group in run["input_groups"]:
                    for uid in group:
                        check(ref["units"][uid]["reserved_by"] == identifier and ref["units"][uid]["status"] == "reserved", "CANCELLATION_MATERIAL_IDENTITY")
                        ref["units"][uid].update(status="quarantined" if run["rework"] else "available", reserved_by=None)
                edit("run", identifier, {"run_status": "aborted", "stop_reason": "cancelled_before_start", "cancelled_at": now}, now)
                check(data == run, "CANCELLED_STATE")
            elif kind == "run.started":
                run = tables["run"][identifier]
                release = valid_release(run["release_id"])
                order = ref["orders"][run["order_id"]]
                check(run["run_status"] == "queued" and date(now) == date(run["scheduled_start"]) and not occupied(run["cell_id"]), "RUN_START_TIME_OR_OCCUPANCY")
                required_updates=[u for u in initial['scheduled_updates'] if order['level']>u.get('after_level',0)] if public.get('instance_revision') == 'factoryops_portfolio150_v1' else (initial['scheduled_updates'] if order['level'] else [])
                check({u['event_id'] for u in required_updates}<={u['event_id'] for u in ref['applied_updates']}, "MISSING_DECLARED_TOOL_UPDATE")
                fixture, cell, tool = tables["fixture"][run["fixture_id"]], tables["robot_cell"][run["cell_id"]], tables["robot_tool"][run["tool_id"]]
                check(cell["status"] == "active" and cell["network_state"] == "online" and fixture["home_pose_verified"] and fixture["occupancy_state"] == "empty" and tool["calibration_status"] == "valid", "RUN_START_EQUIPMENT")
                for group in run["input_groups"]:
                    for uid in group:
                        unit = ref["units"][uid]
                        check(unit["status"] == "reserved" and unit["reserved_by"] == identifier and unit["consumed_by"] is None, "MATERIAL_DOUBLE_SPEND")
                        unit.update(status="consumed", consumed_by=identifier, reserved_by=None)
                edit("run", identifier, {"run_status": "running", "started_at": now, "execution_snapshot": deepcopy(release["snapshot"]),
                    "execution_calibration": deepcopy(ref["calibrations"][release["snapshot"]["tool"]["calibration_id"]])}, now)
                check(data == run, "EXECUTION_SNAPSHOT_LINEAGE")
                bias_at_start[identifier] = ref["tool_biases"][run["tool_id"]]
                edit("fixture", run["fixture_id"], {"occupancy_state": "loaded"}, now)
                edit("robot_cell", run["cell_id"], {"current_program_id": run["program_id"], "mode": "automatic"}, now)
            elif kind == "run.observed":
                run, observed = tables["run"][identifier], data["run"]
                check(run["run_status"] == "running" and run["observed_at"] is None and date(now) >= date(run["scheduled_end"]), "OBSERVATION_PROCESSING_TIME")
                order, snap = ref["orders"][run["order_id"]], run["execution_snapshot"]
                uids, qids, lot_id = observed["output_unit_ids"], observed["quality_check_ids"], observed["output_lot_id"]
                metrics = snap["profile"]["metric_definitions"]
                check(len(uids) == len(set(uids)) == run["planned_cycle_count"] and len(qids) == len(set(qids)) == len(uids) * len(metrics), "MANDATORY_PER_UNIT_METRIC_COVERAGE")
                check(lot_id not in ref["lots"] and all(i not in ref["units"] for i in uids) and all(i not in tables["quality_check"] for i in qids), "OUTPUT_OR_QC_ID_REUSE")
                expected_units, expected_checks = [], []
                for index, (uid, slot, group) in enumerate(zip(uids, run["output_slots"], run["input_groups"])):
                    unit = {"unit_id": uid, "lot_id": lot_id, "order_id": order["order_id"], "level": order["level"], "output_slot": slot, "parent_unit_ids": deepcopy(group),
                        "produced_run_id": identifier, "status": "pending_qc", "quality_accepted": False, "reserved_by": None, "consumed_by": None}
                    expected_units.append(unit)
                    ref["units"][uid] = deepcopy(unit)
                    for j, metric in enumerate(metrics):
                        value = metric["target"] + initial["response_profiles"][order["order_id"]][str(slot)][metric["metric_name"]]
                        value += snap["program"]["process_offset_units"] + bias_at_start[identifier] + snap["tool"]["compensation_units"]
                        if not run["rework"]:
                            value += initial.get("first_pass_deviations", {}).get(order["order_id"], {}).get(str(slot), {}).get(metric["metric_name"], 0)
                        if "coupled_material_policy" in public:
                            for parent_id in run["input_groups"][index]:
                                parent = ref["units"][parent_id]
                                if parent["produced_run_id"]:
                                    producing = tables["run"][parent["produced_run_id"]]
                                    definition = next(m for m in producing["execution_snapshot"]["profile"]["metric_definitions"] if m["metric_name"] == metric["metric_name"])
                                    measured = [c for c in tables["quality_check"].values() if c["unit_id"] == parent_id and c["metric_name"] == metric["metric_name"]]
                                    check(len(measured) == 1, "PARENT_COUPLED_METRIC_EVIDENCE")
                                    value += measured[0]["measured_value"] - definition["target"]
                            pressure, shim = run["pressure_units"][index], run["shim_units"][index]
                            value += pressure + ({"fit_units": 1, "torque_units": -1}[metric["metric_name"]] * shim)
                        elif "material_tolerance_policy" in public:
                            for parent_id in group:
                                parent = ref["units"][parent_id]
                                if parent["produced_run_id"]:
                                    producing = tables["run"][parent["produced_run_id"]]
                                    metric_source = next(m for m in producing["execution_snapshot"]["profile"]["metric_definitions"] if m["metric_name"] == "fit_units")
                                    measured = [c for c in tables["quality_check"].values() if c["unit_id"] == parent_id and c["metric_name"] == "fit_units"]
                                    check(len(measured) == 1, "PARENT_PHYSICAL_FIT_EVIDENCE")
                                    value += measured[0]["measured_value"] - metric_source["target"]
                            value += run["material_offset_units"][index]
                        qid = qids[index * len(metrics) + j]
                        row = {"check_id": qid, "cell_id": run["cell_id"], "run_id": identifier, "profile_id": snap["profile"]["profile_id"], "profile_revision": snap["profile"]["revision"],
                            "unit_id": uid, "output_slot": slot, "metric_name": metric["metric_name"], "measured_value": value, "unit": metric["unit"], "expected_min": metric["expected_min"],
                            "expected_max": metric["expected_max"], "result": "pass" if metric["expected_min"] <= value <= metric["expected_max"] else "fail", "inspector": "environment",
                            "recorded_at": now, "created_at": now, "updated_at": now, "release_id": run["release_id"], "source": "environment_discrete_process", "parent_unit_ids": deepcopy(group), "task_id": public["task_id"]}
                        expected_checks.append(row)
                        tables["quality_check"][qid] = deepcopy(row)
                check(data["units"] == expected_units and data["checks"] == expected_checks, "ACTUAL_PROCESS_MEASUREMENTS_AND_PARENT_LINEAGE")
                ref["lots"][lot_id] = {"lot_id": lot_id, "order_id": order["order_id"], "level": order["level"], "lane": order["lane"], "unit_ids": deepcopy(uids), "produced_run_id": identifier, "status": "pending_qc", "version": 1}
                edit("run", identifier, {"observed_at": now, "quality_check_ids": deepcopy(qids), "output_unit_ids": deepcopy(uids), "output_lot_id": lot_id}, now)
                check(observed == run, "OBSERVED_RUN_STATE")
            elif kind == "run.disposition":
                run = tables["run"][data["run_id"]]
                check(run["run_status"] == "running" and run["observed_at"] is not None and identifier not in ref["dispositions"], "DISPOSITION_REQUIRES_OBSERVATIONS")
                accepted, failed = [], []
                metrics = {m["metric_name"] for m in run["execution_snapshot"]["profile"]["metric_definitions"]}
                for uid in run["output_unit_ids"]:
                    checks = [tables["quality_check"][i] for i in run["quality_check_ids"] if tables["quality_check"][i]["unit_id"] == uid]
                    check(len(checks) == len(metrics) and {c["metric_name"] for c in checks} == metrics, "DISPOSITION_QC_COVERAGE")
                    (accepted if all(c["expected_min"] <= c["measured_value"] <= c["expected_max"] for c in checks) else failed).append(uid)
                expected = {"disposition_id": identifier, "run_id": run["run_id"], "lot_id": run["output_lot_id"], "order_id": run["order_id"], "release_id": run["release_id"],
                    "quality_check_ids": deepcopy(run["quality_check_ids"]), "accepted_unit_ids": accepted, "quarantined_unit_ids": failed, "produced_quantity": len(run["output_unit_ids"]),
                    "defective_unit_count": len(failed), "completed_at": now}
                check(data == expected, "UNIT_DISPOSITION_OR_DEFECT_COUNT")
                ref["dispositions"][identifier] = deepcopy(expected)
                for uid in accepted + failed:
                    ref["units"][uid].update(status="available" if uid in accepted else "quarantined", quality_accepted=uid in accepted)
                ref["lots"][run["output_lot_id"]]["status"] = "accepted" if not failed else "quarantined" if not accepted else "partial"
                edit("run", run["run_id"], {"run_status": "completed", "ended_at": now, "cycle_count": len(run["output_unit_ids"]), "defect_count": len(failed), "disposition_id": identifier}, now)
                edit("fixture", run["fixture_id"], {"occupancy_state": "empty"}, now)
            elif kind == "line.dispatched":
                check(ref["dispatch"] is None and identifier == public["task_id"] and len(ref["applied_updates"]) == len(initial["scheduled_updates"]), "DISPATCH_PRECONDITION")
                check(not any(r["run_status"] in {"queued", "running"} for r in tables["run"].values()), "UNFINISHED_EXECUTION_AT_DISPATCH")
                selected = [ref["units"][i] for i in data["unit_ids"]]
                check(len(selected) == len(set(data["unit_ids"])) and all(u["order_id"] in public["terminal_order_ids"] and u["quality_accepted"] and u["status"] == "available" for u in selected), "DISPATCH_UNIT_IDENTITY")
                totals = {}
                for order_id in public["terminal_order_ids"]:
                    order = ref["orders"][order_id]
                    rows = [u for u in selected if u["order_id"] == order_id]
                    check(len(rows) == order["output_quantity"] and {u["output_slot"] for u in rows} == set(range(order["output_quantity"])), "EXACT_TERMINAL_SLOT_JOIN")
                    totals[order_id] = len(rows)
                dispositions = {tables["run"][u["produced_run_id"]]["disposition_id"] for u in selected}
                check(data["disposition_ids"] == sorted(dispositions) and data["terminal_quantities"] == totals and data["dispatched_at"] == now, "ACTUAL_TERMINAL_DISPOSITION_JOIN")
                check(all(u["unit_id"] in data["unit_ids"] or u["status"] == "consumed" for u in ref["units"].values()), "MATERIAL_CONSERVATION_AT_DISPATCH")
                for unit in selected:
                    unit["status"] = "dispatched"
                ref["dispatch"] = deepcopy(data)
            else:
                check(False, "UNKNOWN_BUSINESS_EVENT")
        except (KeyError, ValueError, TypeError, IndexError, StopIteration, AttributeError):
            errors.append("MISSING_OR_MALFORMED_BUSINESS_EVIDENCE")

    check(date(world["clock"]) >= date(now) and date(world["clock"]) <= date(public["origin"]) + timedelta(seconds=public["policy"]["maximum_clock_seconds"]), "FINAL_CLOCK_BOUND")
    for key in ("tool_biases", "calibrations", "validations", "releases", "dispositions", "lots", "units", "dispatch", "applied_updates"):
        check(ref[key] == state[key], "EVENT_STATE_MISMATCH_" + key.upper())
    for kind, records in tables.items():
        if kind == "audit_log":
            continue
        actual = {r[PRIMARY[kind]]: r for r in world["tables"][kind]}
        check(len(actual) == len(world["tables"][kind]) and actual == records, "ENTITY_STATE_MISMATCH_" + kind.upper())
    audit_events = {a["business_event"]["event_id"]: a for a in world["tables"]["audit_log"] if "business_event" in a}
    check(set(audit_events) == event_ids and len(audit_events) == len(world["tables"]["audit_log"]), "PUBLIC_AUDIT_EVENT_SET")
    for event in state["events"]:
        audit = audit_events.get(event["event_id"], {})
        check(audit.get("business_event") == event and audit.get("entity_id") == event["entity_id"] and audit.get("cell_id") == event["cell_id"] and audit.get("created_at") == event["occurred_at"], "PUBLIC_AUDIT_EVENT_LINEAGE")
    check(ref["dispatch"] is not None, "TERMINAL_HANDOFF_INCOMPLETE")
    outcomes = []
    for order in initial["orders"].values():
        produced = [u for u in ref["units"].values() if u["order_id"] == order["order_id"] and u["quality_accepted"]]
        outcomes.append({"order_id": order["order_id"], "accepted_quantity": len(produced), "required_quantity": order["output_quantity"],
            "quantity_complete": len(produced) == order["output_quantity"] and {u["output_slot"] for u in produced} == set(range(order["output_quantity"]))})
    check(all(o["quantity_complete"] for o in outcomes), "WORK_ORDER_OUTPUT_INCOMPLETE")
    dependency_metrics = {}
    if "material_tolerance_policy" in public or "coupled_material_policy" in public:
        from collections import Counter, defaultdict
        ages, consumers, mixed, edges = Counter(), defaultdict(set), 0, 0
        for run in tables["run"].values():
            if not run["observed_at"] or run["rework"]:
                continue
            target = initial["orders"][run["order_id"]]
            for group in run["input_groups"]:
                origins = set()
                for identifier in group:
                    unit = ref["units"][identifier]
                    if unit["order_id"]:
                        age = target["level"] - unit["level"]
                        ages[age] += 1
                        origins.add(unit["level"])
                        consumers[unit["order_id"]].add(target["order_id"])
                        edges += 1
                mixed += len(origins) > 1
        dependency_metrics = {"actual_interorder_material_edges": edges, "actual_material_origin_age_histogram": dict(sorted(ages.items())),
            "multi_origin_level_groups": mixed, "max_actual_supplier_consumer_fanout": max((len(v) for v in consumers.values()), default=0),
            "nonzero_material_adjustments": sum(v != 0 for r in tables["run"].values() for v in r.get("material_offset_units", [])),
            "declared_order_depth": max(o["level"] for o in initial["orders"].values()) + 1, "minimal_horizon_proven": False,
            "scope": "Declared depth is task-intrinsic. Material edges, ages, mixed groups and supplier fanout use observed non-rework runs only; they exclude same-order rework. Neither declared depth nor reserved setup is completed agent horizon."}
        if "coupled_material_policy" in public:
            dependency_metrics.pop("nonzero_material_adjustments")
            dependency_metrics["coupled_setup"] = {"total_setup_seconds": sum(r.get("setup_seconds", 0) for r in tables["run"].values()),
                "nonzero_pressure_groups": sum(v != 0 for r in tables["run"].values() for v in r.get("pressure_units", [])),
                "nonzero_shim_groups": sum(v != 0 for r in tables["run"].values() for v in r.get("shim_units", [])),
                "scope": "Synthetic coupled controls and actual reserved intervals, not physical process fidelity"}
    return {"task_id": world["task_id"], "accepted": not errors, "errors": sorted(set(errors)), "orders": outcomes,
        "completed_orders": sum(o["quantity_complete"] for o in outcomes), "required_orders": len(outcomes), "actual_attempts": len(tables["run"]),
        "rework_attempts": sum(r["rework"] for r in tables["run"].values()), "observed_checks": len(tables["quality_check"]),
        "provider_calls": 0, "evidence_kind": "independent_synthetic_manufacturing_state_not_model_behavior",
        **({"dependency_metrics": dependency_metrics} if dependency_metrics else {})}
