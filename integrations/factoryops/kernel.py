"""Discrete production semantics over the original manufacturing entities."""

from copy import deepcopy
from collections import Counter
from datetime import datetime, timedelta, timezone
import importlib
import json
from pathlib import Path
from unittest.mock import patch

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from integrations.internal_mcp.parameter_contract import apply_public_enums
from .build import KEYS


READS = {"list_robot_cells", "get_cell_overview", "list_programs", "inspect_program_detail", "list_waypoints", "list_robot_tools",
    "list_safety_zones", "search_runs", "list_quality_checks", "inspect_audit_trail"}


class BusinessError(Exception):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = deepcopy(details) if details is not None else {}


def require(condition, code, details=None):
    if not condition:
        raise BusinessError(code, details)


def instant(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    require(result.tzinfo is not None, "TIMEZONE_REQUIRED")
    return result.astimezone(timezone.utc)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


class Production:
    def __init__(self, task, runtime):
        self.task, self.runtime = Path(task).resolve(), Path(runtime).resolve()
        self.source = controller_source(self.task)
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Use an isolated runtime")
        self.backend = load_backend(self.source)
        self.logic = importlib.import_module("mcp_servers.tool_core")
        self.public = read_json(self.task / "public/task.json")
        self.world = read_json(self.runtime / "world.json")
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        self.sm = TableState(self.world, read_json(self.task / "background.json"), KEYS)
        self.metadata = {name: deepcopy(self.backend._TOOL_METADATA[name]) for name in sorted(READS)}
        for entry in self.metadata.values():
            entry["input_schema"]["additionalProperties"] = False
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 256}
        offset = {"type": "integer", "minimum": -10, "maximum": 10}
        definitions = {
            "read_production_order": ("Read an actual work order, BOM and current material/run state.", schema({"order_id": text})),
            "read_material_inventory": ("Read actual task material lots and unit identities, including reserved, consumed and quarantined units.", schema({"order_id": text}, [])),
            "read_production_clock": ("Read the actual simulation time and outstanding equipment reservations.", schema({})),
            "advance_production_clock": ("Advance time without fabricating observations or completing a batch.", schema({"to_time": text})),
            "create_program_revision": ("Create a separate draft and waypoint copy; preserve every old release and execution attempt.", schema({"source_program_id": text, "new_name": text, "actor": text})),
            "update_program_metadata": ("Edit a draft or validated program and invalidate its validation. Released snapshots cannot be edited.", schema({"program_id": text,
                "cycle_time_sec": {"type": "integer", "minimum": 5, "maximum": 60}, "process_offset_units": offset, "actor": text}, ["program_id", "actor"])),
            "update_waypoint_pose": ("Edit an unreleased waypoint's position or speed and invalidate the parent validation.", schema({"waypoint_id": text,
                "pose": schema({k: {"type": "integer", "minimum": -500 if k.endswith("mm") else -180, "maximum": 500 if k.endswith("mm") else 180}
                    for k in ("x_mm", "y_mm", "z_mm", "rx_deg", "ry_deg", "rz_deg")}),
                "speed_mm_s": {"type": "integer", "minimum": 1, "maximum": 1000}, "actor": text}, ["waypoint_id", "actor"])),
            "measure_tool_calibration": ("Perform an environment-owned calibration test for the actual tool revision; actor supplies no measurement.", schema({"tool_id": text})),
            "read_tool_calibration": ("Read a performed calibration measurement.", schema({"calibration_id": text})),
            "update_tool_calibration": ("Apply actor-computed compensation using a current measured calibration; cannot write a valid flag.", schema({"tool_id": text, "calibration_id": text, "compensation_units": offset, "actor": text})),
            "validate_production_program": ("Validate current program content, capacity, waypoints, calibration and mandatory QC profile; not robot dynamics.", schema({"program_id": text})),
            "read_program_validation": ("Read actual version-bound validation and its public checks.", schema({"validation_id": text})),
            "publish_program_release": ("Publish an immutable exact snapshot using its passing current validation.", schema({"program_id": text, "validation_id": text, "actor": text})),
            "read_program_release": ("Read immutable released program and equipment/profile evidence.", schema({"release_id": text})),
            "schedule_program_run": ("Reserve a named work order, exact release, equipment interval and material groups. Each group produces one output unit.", schema({"order_id": text,
                "release_id": text, "start_at": text, "input_groups": {"type": "array", "minItems": 1, "maxItems": 128, "items": {**ids, "minItems": 1, "maxItems": 2}},
                "rework": {"type": "boolean"}, "operator_name": text}, ["order_id", "release_id", "start_at", "input_groups", "operator_name"])),
            "read_program_run": ("Read an actual execution attempt, reservations and input/output identities.", schema({"run_id": text})),
            "cancel_program_run": ("Cancel only an unstarted reservation and release its unconsumed material; no history reset.", schema({"run_id": text, "actor": text})),
            "execute_program_run": ("Start the reserved attempt at its scheduled time after rechecking current release and exact materials.", schema({"run_id": text, "actor": text})),
            "collect_run_observations": ("After processing time, create actual per-unit mandatory QC observations from the immutable execution snapshot.", schema({"run_id": text})),
            "read_run_observations": ("Read actual per-unit measurements and source identities; no hidden expected labels are returned.", schema({"run_id": text})),
            "finalize_program_run": ("Validate actor-computed unit disposition against every observed metric; derives actual quantity and defective-unit count.", schema({"run_id": text,
                "accepted_unit_ids": ids, "quarantined_unit_ids": ids, "operator_name": text})),
            "read_lot_disposition": ("Read the immutable batch disposition, including failed units and exact checks.", schema({"disposition_id": text})),
            "poll_production_updates": ("Poll the declared post-first-layer tooling drift; invalidates future dependent releases, not completed runs.", schema({})),
            "reconcile_production_handoff": ("Dispatch the exact terminal material units and their actual dispositions after verifying full material conservation.", schema({"unit_ids": ids, "disposition_ids": ids})),
        }
        self.metadata.update({name: {"description": d, "input_schema": s} for name, (d, s) in definitions.items()})
        apply_public_enums("C50", self.metadata)
        if "coupled_material_policy" in self.public:
            inputs = self.metadata["schedule_program_run"]["input_schema"]
            inputs["properties"]["input_groups"]["items"]["maxItems"] = max(
                4, max(order["components_per_output"] for order in self.public["orders"]))
            for key in ("pressure_units", "shim_units"):
                inputs["properties"][key] = {"type": "array", "maxItems": 128, "items": {"type": "integer", "minimum": -10, "maximum": 10}}
                inputs["required"].append(key)
        elif "material_tolerance_policy" in self.public:
            inputs = self.metadata["schedule_program_run"]["input_schema"]
            inputs["properties"]["input_groups"]["items"]["maxItems"] = max(
                4, max(order["components_per_output"] for order in self.public["orders"]))
            inputs["properties"]["material_offset_units"] = {"type": "array", "maxItems": 128, "items": {"type": "integer", "minimum": -10, "maximum": 10}}
            inputs["required"].append("material_offset_units")
        self.validate_state()

    @property
    def state(self):
        return self.world["production"]

    def now(self):
        return self.world["clock"]

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        from integrations.internal_mcp.topology import runtime_identifier
        return runtime_identifier(self.public, kind, value, f"{self.world['task_id']}-{kind}-{value}")

    def owned(self, kind, identifier):
        if kind in {"order", "lot", "unit"}:
            group = {"order": "orders", "lot": "lots", "unit": "units"}[kind]
            require(identifier in self.state[group], kind.upper() + "_NOT_FOUND")
            return deepcopy(self.state[group][identifier])
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, kind.upper() + "_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def update(self, kind, identifier, values):
        return self.sm.update_entity(kind, identifier, {**values, "updated_at": self.now()})

    def native(self, name, args):
        with patch.object(self.logic, "now_iso", self.now):
            result = self.backend.call_tool(name, state_manager=self.sm, **args)["content"]
        if result.get("message", "").startswith("INTERNAL_ERROR"):
            raise RuntimeError(result["message"])
        require(result["status"] == "success", result.get("message", "NATIVE_TOOL_ERROR"))
        return result["data"]

    def event(self, kind, identifier, cell_id, data, *, environment=False):
        event = {"event_id": self.next_id("event"), "type": kind, "entity_id": identifier, "cell_id": cell_id,
            "sequence": self.world["sequence"], "occurred_at": self.now(), "data": deepcopy(data)}
        self.state["events"].append(event)
        action = "quality_record" if kind in {"run.observed", "run.disposition"} else "tool_mount" if kind.startswith("calibration.") else "run_execute" if kind.startswith("run.") else "program_update"
        self.sm.add_entity("audit_log", {"audit_id": event["event_id"] + "-audit", "cell_id": cell_id, "actor": "environment" if environment else self.public["actor"],
            "action_type": action, "entity_type": kind.split(".")[0], "entity_id": identifier, "summary": kind + ": " + identifier,
            "created_at": self.now(), "business_event": deepcopy(event)})

    def snapshot(self, program_id):
        program = self.owned("program", program_id)
        keys = ("program_id", "order_id", "cell_id", "version", "cycle_time_sec", "payload_kg", "process_offset_units", "active_tool_id", "quality_profile_id", "waypoint_ids", "safety_zone_ids")
        tool = self.owned("robot_tool", program["active_tool_id"])
        return {"program": {k: deepcopy(program[k]) for k in keys},
            "waypoints": [{k: w[k] for k in ("waypoint_id", "program_id", "cell_id", "sequence_index", "pose", "speed_mm_s", "blend_radius_mm", "approach")}
                for w in [self.owned("waypoint", i) for i in program["waypoint_ids"]]],
            "tool": {k: deepcopy(tool[k]) for k in ("tool_id", "cell_id", "revision", "payload_kg", "calibration_status", "calibration_id", "compensation_units")},
            "zones": [{k: z[k] for k in ("zone_id", "cell_id", "revision", "enabled", "severity", "max_speed_mm_s", "polygon_points")}
                for z in [self.owned("safety_zone", i) for i in program["safety_zone_ids"]]],
            "profile": self.owned("quality_check_profile", program["quality_profile_id"])}

    def release(self, identifier, *, current=False):
        require(identifier in self.state["releases"], "RELEASE_NOT_FOUND")
        release = deepcopy(self.state["releases"][identifier])
        if current:
            require(release["snapshot"] == self.snapshot(release["program_id"]), "STALE_PROGRAM_OR_EQUIPMENT_RELEASE")
            require(self.owned("program", release["program_id"])["status"] == "released", "PROGRAM_NOT_RELEASED")
        return release

    def available_output(self, order_id):
        return [deepcopy(u) for u in self.state["units"].values() if u["order_id"] == order_id and u["quality_accepted"]]

    def busy(self, cell_id):
        return any(r["cell_id"] == cell_id and r["run_status"] == "running" for r in self.world["tables"]["run"])

    def cell_occupancy(self, cell_id):
        fields = ("run_id", "order_id", "run_status", "scheduled_end", "observed_at", "disposition_id")
        return {"cell_id": cell_id, "occupying_runs": [
            {k: deepcopy(r[k]) for k in fields}
            for r in self.world["tables"]["run"] if r["cell_id"] == cell_id and r["run_status"] == "running"]}

    def update_prerequisites(self, planned):
        after_level = planned.get("after_level", 0) if self.public.get("instance_revision") == "factoryops_portfolio150_v1" else 0
        incomplete = []
        for order in self.state["orders"].values():
            if order["level"] <= after_level:
                # Accepted upstream outputs still count after downstream consumption.
                accepted = len(self.available_output(order["order_id"]))
                if accepted != order["output_quantity"]:
                    incomplete.append({"order_id": order["order_id"], "level": order["level"],
                        "accepted_quantity": accepted, "required_quantity": order["output_quantity"]})
        return {"event_id": planned["event_id"], "after_level": after_level,
            "incomplete_orders": sorted(incomplete, key=lambda o: (o["level"], o["order_id"]))}

    def dispatch(self, name, args):
        if name in READS:
            data = self.native(name, args)
            if name == "list_quality_checks":
                for row in data["checks"]:
                    actual = self.sm.get_entity("quality_check", row["check_id"])
                    if actual.get("task_id") == self.world["task_id"]:
                        row.update({key: deepcopy(actual[key]) for key in ("unit_id", "output_slot", "release_id", "profile_revision", "parent_unit_ids", "source")})
            return data
        if "actor" in args:
            require(args["actor"] == self.public["actor"], "ACTOR_IDENTITY")
        if "operator_name" in args:
            require(args["operator_name"] == self.public["actor"], "OPERATOR_IDENTITY")
        if name == "read_production_order":
            order = self.owned("order", args["order_id"])
            return {"order": order, "output_units": self.available_output(order["order_id"]),
                "runs": [deepcopy(r) for r in self.world["tables"]["run"] if r["order_id"] == order["order_id"]]}
        if name == "read_material_inventory":
            if args.get("order_id"):
                self.owned("order", args["order_id"])
            lots = [deepcopy(l) for l in self.state["lots"].values() if not args.get("order_id") or l["order_id"] == args["order_id"]]
            return {"lots": lots, "units": [deepcopy(self.state["units"][i]) for lot in lots for i in lot["unit_ids"]]}
        if name == "read_production_clock":
            return {"now": self.now(), "reservations": [deepcopy(r) for r in self.world["tables"]["run"] if r["run_status"] in {"queued", "running"}]
                + deepcopy(self.public.get("equipment_calendar", []))}
        if name == "advance_production_clock":
            try:
                target = instant(args["to_time"])
            except (ValueError, TypeError, AttributeError):
                raise BusinessError("INVALID_TIMESTAMP") from None
            require(instant(self.now()) <= target <= instant(self.public["origin"]) + timedelta(seconds=self.public["policy"]["maximum_clock_seconds"]), "CLOCK_BOUND")
            self.world["clock"] = target.isoformat().replace("+00:00", "Z")
            return {"now": self.now()}
        if name == "create_program_revision":
            return self.revise(args)
        if name == "update_program_metadata":
            row = self.owned("program", args["program_id"])
            require(row["status"] in {"draft", "validated"}, "RELEASED_PROGRAM_IMMUTABLE")
            values = {k: args[k] for k in ("cycle_time_sec", "process_offset_units") if k in args}
            require(bool(values), "MISSING_MUTATION")
            if all(row[k] == v for k, v in values.items()):
                return row
            updated = self.update("program", row["program_id"], {**values, "version": row["version"] + 1, "status": "draft", "validation_id": None})
            self.event("program.edited", row["program_id"], row["cell_id"], {"before": row, "after": updated})
            return updated
        if name == "update_waypoint_pose":
            waypoint = self.owned("waypoint", args["waypoint_id"])
            program = self.owned("program", waypoint["program_id"])
            require(program["status"] in {"draft", "validated"} and not waypoint["locked"], "RELEASED_WAYPOINT_IMMUTABLE")
            values = {k: args[k] for k in ("pose", "speed_mm_s") if k in args}
            require(bool(values), "MISSING_MUTATION")
            changed = self.update("waypoint", waypoint["waypoint_id"], values)
            parent = self.update("program", program["program_id"], {"version": program["version"] + 1, "status": "draft", "validation_id": None})
            self.event("waypoint.edited", waypoint["waypoint_id"], waypoint["cell_id"], {"before": waypoint, "after": changed, "program": parent})
            return {"waypoint": changed, "program": parent}
        if name == "measure_tool_calibration":
            tool = self.owned("robot_tool", args["tool_id"])
            require(not self.busy(tool["cell_id"]), "CELL_OCCUPIED")
            identifier = self.next_id("calibration")
            record = {"calibration_id": identifier, "tool_id": tool["tool_id"], "tool_revision": tool["revision"],
                "observed_bias_units": self.state["tool_biases"][tool["tool_id"]], "measured_at": self.now(), "source": "environment_calibration_test"}
            self.state["calibrations"][identifier] = record
            self.event("calibration.measured", identifier, tool["cell_id"], record, environment=True)
            return deepcopy(record)
        if name == "read_tool_calibration":
            require(args["calibration_id"] in self.state["calibrations"], "CALIBRATION_NOT_FOUND")
            return deepcopy(self.state["calibrations"][args["calibration_id"]])
        if name == "update_tool_calibration":
            tool = self.owned("robot_tool", args["tool_id"])
            require(not self.busy(tool["cell_id"]), "CELL_OCCUPIED")
            require(args["calibration_id"] in self.state["calibrations"], "CALIBRATION_NOT_FOUND")
            measured = self.state["calibrations"][args["calibration_id"]]
            require(measured["tool_id"] == tool["tool_id"] and measured["tool_revision"] == tool["revision"], "CALIBRATION_TOOL_REVISION_MISMATCH")
            require(args["compensation_units"] == -measured["observed_bias_units"], "COMPENSATION_MISMATCH")
            updated = self.update("robot_tool", tool["tool_id"], {"calibration_status": "valid", "calibration_id": measured["calibration_id"],
                "compensation_units": args["compensation_units"], "last_calibrated_at": self.now()})
            self.event("calibration.applied", tool["tool_id"], tool["cell_id"], {"before": tool, "after": updated})
            return updated
        if name == "validate_production_program":
            return self.validate_program(args["program_id"])
        if name == "read_program_validation":
            require(args["validation_id"] in self.state["validations"], "VALIDATION_NOT_FOUND")
            return deepcopy(self.state["validations"][args["validation_id"]])
        if name == "publish_program_release":
            program = self.owned("program", args["program_id"])
            require(args["validation_id"] in self.state["validations"], "VALIDATION_NOT_FOUND")
            validation = self.state["validations"][args["validation_id"]]
            require(validation["program_id"] == program["program_id"] and validation["passed"] and validation["snapshot"] == self.snapshot(program["program_id"]), "PASSING_CURRENT_VALIDATION_REQUIRED")
            if program["release_id"]:
                return self.release(program["release_id"], current=True)
            require(program["status"] == "validated", "PROGRAM_NOT_VALIDATED")
            identifier = self.next_id("release")
            record = {"release_id": identifier, "program_id": program["program_id"], "validation_id": validation["validation_id"], "snapshot": deepcopy(validation["snapshot"]), "released_at": self.now()}
            self.state["releases"][identifier] = record
            self.update("program", program["program_id"], {"status": "released", "release_id": identifier})
            for waypoint in program["waypoint_ids"]:
                self.update("waypoint", waypoint, {"locked": True})
            self.event("program.released", identifier, program["cell_id"], record)
            return deepcopy(record)
        if name == "read_program_release":
            return self.release(args["release_id"])
        if name == "schedule_program_run":
            return self.schedule(args)
        if name == "read_program_run":
            return self.owned("run", args["run_id"])
        if name == "cancel_program_run":
            run = self.owned("run", args["run_id"])
            require(run["run_status"] == "queued", "ONLY_UNSTARTED_RESERVATIONS_CAN_CANCEL")
            for group in run["input_groups"]:
                for identifier in group:
                    unit = self.state["units"][identifier]
                    require(unit["reserved_by"] == run["run_id"] and unit["status"] == "reserved", "MATERIAL_RESERVATION_MISMATCH")
                    unit.update(status="quarantined" if run["rework"] else "available", reserved_by=None)
            updated = self.update("run", run["run_id"], {"run_status": "aborted", "stop_reason": "cancelled_before_start", "cancelled_at": self.now()})
            self.event("run.cancelled", run["run_id"], run["cell_id"], updated)
            return updated
        if name == "execute_program_run":
            return self.execute_run(args["run_id"])
        if name == "collect_run_observations":
            return self.observe(args["run_id"])
        if name == "read_run_observations":
            run = self.owned("run", args["run_id"])
            require(run.get("observed_at") is not None, "RUN_NOT_OBSERVED")
            return {"run": run, "checks": [self.owned("quality_check", identifier) for identifier in run["quality_check_ids"]],
                "units": [self.owned("unit", identifier) for identifier in run["output_unit_ids"]]}
        if name == "finalize_program_run":
            return self.finalize(args)
        if name == "read_lot_disposition":
            require(args["disposition_id"] in self.state["dispositions"], "DISPOSITION_NOT_FOUND")
            return deepcopy(self.state["dispositions"][args["disposition_id"]])
        if name == "poll_production_updates":
            return self.poll_updates()
        if name == "reconcile_production_handoff":
            return self.handoff(args)
        raise BusinessError("TOOL_NOT_EXPOSED")

    def revise(self, args):
        source = self.owned("program", args["source_program_id"])
        require(not any(p["name"] == args["new_name"] and p["cell_id"] == source["cell_id"] for p in self.world["tables"]["program"]), "DUPLICATE_PROGRAM_NAME")
        identifier = self.next_id("program")
        waypoints = []
        for original in source["waypoint_ids"]:
            row = self.owned("waypoint", original)
            wid = self.next_id("waypoint")
            self.sm.add_entity("waypoint", {**row, "waypoint_id": wid, "program_id": identifier, "locked": False, "created_at": self.now(), "updated_at": self.now()})
            waypoints.append(wid)
        new = {**source, "program_id": identifier, "name": args["new_name"], "version": source["version"] + 1, "source_program_id": source["program_id"],
            "status": "draft", "waypoint_ids": waypoints, "validation_id": None, "release_id": None, "created_at": self.now(), "updated_at": self.now()}
        self.sm.add_entity("program", new)
        tool = self.owned("robot_tool", new["active_tool_id"])
        self.update("robot_tool", tool["tool_id"], {"compatible_program_ids": tool["compatible_program_ids"] + [identifier]})
        for zid in new["safety_zone_ids"]:
            zone = self.owned("safety_zone", zid)
            self.update("safety_zone", zid, {"linked_program_ids": zone["linked_program_ids"] + [identifier]})
        self.event("program.revised", identifier, source["cell_id"], {"source_program_id": source["program_id"], "program": new,
            "waypoints": [self.owned("waypoint", i) for i in waypoints]})
        return self.owned("program", identifier)

    def validate_program(self, identifier):
        program = self.owned("program", identifier)
        snapshot = self.snapshot(identifier)
        tool, profile, zones, points = (snapshot[k] for k in ("tool", "profile", "zones", "waypoints"))
        cell = self.owned("robot_cell", program["cell_id"])
        checks = {"cell_available": cell["status"] == "active" and cell["network_state"] == "online",
            "tool_current_calibration": tool["calibration_status"] == "valid" and tool["calibration_id"] in self.state["calibrations"],
            "same_cell": all(row["cell_id"] == program["cell_id"] for row in [tool, profile] + zones + points),
            "tool_payload": tool["payload_kg"] >= program["payload_kg"], "mandatory_profile": profile["status"] == "active" and len(profile["metric_definitions"]) == 2,
            "ordered_waypoints": len(points) >= 2 and [p["sequence_index"] for p in points] == list(range(1, len(points) + 1)),
            "protective_envelope": bool(zones) and all(z["enabled"] and z["severity"] in {"protective_stop", "emergency_stop"} for z in zones),
            "speed_within_envelope": bool(zones) and all(p["speed_mm_s"] <= min(z["max_speed_mm_s"] for z in zones) for p in points),
            "coordinate_envelope": all(-500 <= p["pose"][k] <= 500 for p in points for k in ("x_mm", "y_mm", "z_mm"))}
        if tool["calibration_id"] in self.state["calibrations"]:
            calibration = self.state["calibrations"][tool["calibration_id"]]
            checks["tool_current_calibration"] &= calibration["tool_id"] == tool["tool_id"] and calibration["tool_revision"] == tool["revision"] and tool["compensation_units"] == -calibration["observed_bias_units"]
        validation_id = self.next_id("validation")
        record = {"validation_id": validation_id, "program_id": identifier, "snapshot": snapshot, "checks": checks, "passed": all(checks.values()), "validated_at": self.now()}
        self.state["validations"][validation_id] = record
        if program["status"] != "released":
            self.update("program", identifier, {"status": "validated" if record["passed"] else "draft", "validation_id": validation_id})
        self.event("program.validated", validation_id, program["cell_id"], record, environment=True)
        return deepcopy(record)

    def schedule(self, args):
        order = self.owned("order", args["order_id"])
        release = self.release(args["release_id"], current=True)
        program = release["snapshot"]["program"]
        require(program["order_id"] == order["order_id"] and program["cell_id"] == order["cell_id"], "WORK_ORDER_RELEASE_IDENTITY")
        try:
            start = instant(args["start_at"])
        except (ValueError, TypeError, AttributeError):
            raise BusinessError("INVALID_TIMESTAMP") from None
        require(start >= instant(self.now()), "RESERVATION_IN_PAST")
        groups, rework = args["input_groups"], args.get("rework", False)
        flat = [i for group in groups for i in group]
        require(len(flat) == len(set(flat)), "DUPLICATE_MATERIAL_UNIT")
        attempts = [r for r in self.world["tables"]["run"] if r["order_id"] == order["order_id"] and r.get("stop_reason") != "cancelled_before_start"]
        require(order["max_attempts"] is None or len(attempts) < order["max_attempts"], "WORK_ORDER_ATTEMPT_LIMIT")
        require(rework or not attempts, "EXISTING_PRIMARY_ATTEMPT_REQUIRES_EXPLICIT_REWORK")
        require(rework or len(groups) == order["output_quantity"], "PLANNED_QUANTITY_MISMATCH")
        slots = []
        for index, group in enumerate(groups):
            units = [self.owned("unit", i) for i in group]
            if rework:
                require(len(units) == 1 and units[0]["order_id"] == order["order_id"] and units[0]["status"] == "quarantined" and not units[0]["quality_accepted"], "REWORK_REQUIRES_OWN_QUARANTINED_OUTPUT")
                slots.append(units[0]["output_slot"])
            else:
                require(all(u["status"] == "available" and u["quality_accepted"] for u in units), "MATERIAL_NOT_ACCEPTED_AND_AVAILABLE")
                if order["level"]:
                    if "bom" in order:
                        from collections import Counter
                        require(Counter(u["order_id"] for u in units) == {c["parent_order_id"]: c["units_per_output"] for c in order["bom"]}
                            and all(u["level"] == self.state["orders"][u["order_id"]]["level"] < order["level"] for u in units), "BOM_PARENT_QUANTITY_AND_ORIGIN_MISMATCH")
                    else:
                        require(len(units) == 2 and {u["order_id"] for u in units} == set(order["parent_order_ids"]) and all(u["level"] == order["level"] - 1 for u in units), "BOM_PARENT_JOIN_MISMATCH")
                else:
                    require(len(units) == 1 and units[0]["lot_id"] in order["raw_lot_ids"] and units[0]["level"] == -1, "ROOT_MATERIAL_IDENTITY")
                slots.append(index)
        require(len(set(slots)) == len(slots), "DUPLICATE_OUTPUT_SLOT")
        setup = 0
        if "coupled_material_policy" in self.public:
            policy = self.public["coupled_material_policy"]
            require(len(args["pressure_units"]) == len(args["shim_units"]) == len(groups), "PER_GROUP_COUPLED_CONTROL_COVERAGE")
            setup = sum(abs(p) * policy["pressure_seconds_per_unit"] + abs(s) * policy["shim_seconds_per_unit"] for p, s in zip(args["pressure_units"], args["shim_units"]))
        elif "material_tolerance_policy" in self.public:
            require(len(args["material_offset_units"]) == len(groups), "PER_GROUP_MATERIAL_OFFSET_COVERAGE")
        end = start + timedelta(seconds=program["cycle_time_sec"] * len(groups) + setup)
        require(end <= instant(self.public["origin"]) + timedelta(seconds=self.public["policy"]["maximum_clock_seconds"]), "RESERVATION_OUTSIDE_HORIZON")
        for reservation in self.public.get("equipment_calendar", []):
            if any(reservation[k] == order[k] for k in ("cell_id", "tool_id", "fixture_id")):
                require(end <= instant(reservation["scheduled_start"]) or start >= instant(reservation["scheduled_end"]), "EQUIPMENT_MAINTENANCE_COLLISION",
                    {"requested_interval": {"start": args["start_at"], "end": end.isoformat()},
                     "shared_resources": {k: order[k] for k in ("cell_id", "tool_id", "fixture_id") if reservation[k] == order[k]},
                     "conflicting_reservation": {k: deepcopy(reservation[k]) for k in ("reservation_id", "scheduled_start", "scheduled_end") if k in reservation}})
        for prior in self.world["tables"]["run"]:
            shared = any(prior[k] == order[k] for k in ("cell_id", "tool_id", "fixture_id"))
            if shared and prior["run_status"] in {"queued", "running"}:
                require(end <= instant(prior["scheduled_start"]) or start >= instant(prior["scheduled_end"]), "EQUIPMENT_INTERVAL_COLLISION",
                    {"requested_interval": {"start": args["start_at"], "end": end.isoformat()},
                     "shared_resources": {k: order[k] for k in ("cell_id", "tool_id", "fixture_id") if prior[k] == order[k]},
                     "conflicting_reservation": {k: deepcopy(prior[k]) for k in ("run_id", "order_id", "run_status", "scheduled_start", "scheduled_end")}})
        identifier = self.next_id("run")
        record = {"run_id": identifier, "task_id": self.world["task_id"], "cell_id": order["cell_id"], "program_id": program["program_id"], "order_id": order["order_id"],
            "operator_name": args["operator_name"], "run_status": "queued", "started_at": None, "ended_at": None, "cycle_count": 0, "defect_count": 0,
            "stop_reason": None, "fixture_id": order["fixture_id"], "tool_id": order["tool_id"], "quality_check_ids": [], "safety_event_ids": [],
            "planned_cycle_count": len(groups), "input_groups": deepcopy(groups), "output_slots": slots, "input_evidence": [self.owned("unit", i) for i in flat],
            "release_id": release["release_id"], "scheduled_start": start.isoformat().replace("+00:00", "Z"), "scheduled_end": end.isoformat().replace("+00:00", "Z"),
            "rework": rework, "observed_at": None, "output_unit_ids": [], "output_lot_id": None, "disposition_id": None, "created_at": self.now(), "updated_at": self.now()}
        if "coupled_material_policy" in self.public:
            record.update(pressure_units=deepcopy(args["pressure_units"]), shim_units=deepcopy(args["shim_units"]), setup_seconds=setup)
        elif "material_tolerance_policy" in self.public:
            record["material_offset_units"] = deepcopy(args["material_offset_units"])
        self.sm.add_entity("run", record)
        for identifier_unit in flat:
            self.state["units"][identifier_unit].update(status="reserved", reserved_by=identifier)
        self.event("run.scheduled", identifier, order["cell_id"], record)
        return self.owned("run", identifier)

    def execute_run(self, identifier):
        run = self.owned("run", identifier)
        require(run["run_status"] == "queued", "RUN_NOT_QUEUED")
        require(instant(self.now()) == instant(run["scheduled_start"]), "SCHEDULED_START_TIME_REQUIRED",
            {"run_id": identifier, "current_time": self.now(), "required_start": run["scheduled_start"],
             "timing": "early" if instant(self.now()) < instant(run["scheduled_start"]) else "late",
             "comparison": "current_time == required_start"})
        release = self.release(run["release_id"], current=True)
        order = self.owned("order", run["order_id"])
        if order["level"] > 0:
            needed = [u for u in self.state["scheduled_updates"] if order["level"] > u.get("after_level", 0)] if self.public.get('instance_revision') == 'factoryops_portfolio150_v1' else self.state["scheduled_updates"]
            applied = {u['event_id'] for u in self.state['applied_updates']}
            if not {u['event_id'] for u in needed} <= applied:
                raise BusinessError("DECLARED_TOOL_UPDATE_NOT_POLLED", {"run_id": identifier,
                    "pending_updates": [self.update_prerequisites(u) for u in needed if u['event_id'] not in applied]})
        if self.busy(run["cell_id"]):
            raise BusinessError("CELL_STILL_OCCUPIED", self.cell_occupancy(run["cell_id"]))
        cell, fixture, tool = self.owned("robot_cell", run["cell_id"]), self.owned("fixture", run["fixture_id"]), self.owned("robot_tool", run["tool_id"])
        require(cell["status"] == "active" and cell["network_state"] == "online" and fixture["home_pose_verified"] and fixture["occupancy_state"] == "empty", "EQUIPMENT_NOT_READY")
        require(tool["calibration_status"] == "valid", "TOOL_CALIBRATION_REQUIRED")
        for group in run["input_groups"]:
            for uid in group:
                unit = self.state["units"][uid]
                require(unit["status"] == "reserved" and unit["reserved_by"] == identifier and unit["consumed_by"] is None, "MATERIAL_RESERVATION_MISMATCH")
                unit.update(status="consumed", consumed_by=identifier, reserved_by=None)
        result = self.update("run", identifier, {"run_status": "running", "started_at": self.now(), "execution_snapshot": deepcopy(release["snapshot"]),
            "execution_calibration": deepcopy(self.state["calibrations"][release["snapshot"]["tool"]["calibration_id"]])})
        self.update("fixture", fixture["fixture_id"], {"occupancy_state": "loaded"})
        self.update("robot_cell", cell["cell_id"], {"current_program_id": run["program_id"], "mode": "automatic"})
        self.event("run.started", identifier, run["cell_id"], result)
        return result

    def observe(self, identifier):
        run = self.owned("run", identifier)
        if run["observed_at"] is not None:
            return self.dispatch("read_run_observations", {"run_id": identifier})
        require(run["run_status"] == "running" and instant(self.now()) >= instant(run["scheduled_end"]), "PROCESSING_NOT_FINISHED")
        order = self.owned("order", run["order_id"])
        snapshot = run["execution_snapshot"]
        lot_id = self.next_id("lot")
        output_ids, checks = [], []
        for index, (slot, group) in enumerate(zip(run["output_slots"], run["input_groups"])):
            uid = self.next_id("unit")
            output_ids.append(uid)
            self.state["units"][uid] = {"unit_id": uid, "lot_id": lot_id, "order_id": order["order_id"], "level": order["level"], "output_slot": slot,
                "parent_unit_ids": deepcopy(group), "produced_run_id": identifier, "status": "pending_qc", "quality_accepted": False, "reserved_by": None, "consumed_by": None}
            for metric in snapshot["profile"]["metric_definitions"]:
                value = metric["target"] + self.state["response_profiles"][order["order_id"]][str(slot)][metric["metric_name"]] + snapshot["program"]["process_offset_units"]
                value += run["execution_calibration"]["observed_bias_units"] + snapshot["tool"]["compensation_units"]
                if not run["rework"]:
                    value += self.state.get("first_pass_deviations", {}).get(order["order_id"], {}).get(str(slot), {}).get(metric["metric_name"], 0)
                if "coupled_material_policy" in self.public:
                    inherited = 0
                    for parent_id in group:
                        parent = self.state["units"][parent_id]
                        if parent["produced_run_id"]:
                            producer = self.owned("run", parent["produced_run_id"])
                            target = next(m["target"] for m in producer["execution_snapshot"]["profile"]["metric_definitions"] if m["metric_name"] == metric["metric_name"])
                            measurements = [c for c in self.world["tables"]["quality_check"] if c["unit_id"] == parent_id and c["metric_name"] == metric["metric_name"]]
                            require(len(measurements) == 1, "ACTUAL_PARENT_COUPLED_MEASUREMENTS_REQUIRED")
                            inherited += measurements[0]["measured_value"] - target
                    value += inherited + run["pressure_units"][index] + (run["shim_units"][index] if metric["metric_name"] == "fit_units" else -run["shim_units"][index])
                elif "material_tolerance_policy" in self.public:
                    inherited = 0
                    for parent_id in group:
                        parent = self.state["units"][parent_id]
                        if parent["produced_run_id"]:
                            producer = self.owned("run", parent["produced_run_id"])
                            target = next(m["target"] for m in producer["execution_snapshot"]["profile"]["metric_definitions"] if m["metric_name"] == "fit_units")
                            measurements = [c for c in self.world["tables"]["quality_check"] if c["unit_id"] == parent_id and c["metric_name"] == "fit_units"]
                            require(len(measurements) == 1, "ACTUAL_PARENT_FIT_MEASUREMENT_REQUIRED")
                            inherited += measurements[0]["measured_value"] - target
                    value += inherited + run["material_offset_units"][index]
                check_id = self.next_id("check")
                passed = metric["expected_min"] <= value <= metric["expected_max"]
                check = {"check_id": check_id, "cell_id": run["cell_id"], "run_id": identifier, "profile_id": snapshot["profile"]["profile_id"],
                    "profile_revision": snapshot["profile"]["revision"], "unit_id": uid, "output_slot": slot, "metric_name": metric["metric_name"], "measured_value": value,
                    "unit": metric["unit"], "expected_min": metric["expected_min"], "expected_max": metric["expected_max"], "result": "pass" if passed else "fail",
                    "inspector": "environment", "recorded_at": self.now(), "created_at": self.now(), "updated_at": self.now(), "release_id": run["release_id"],
                    "source": "environment_discrete_process", "parent_unit_ids": deepcopy(group)}
                self.sm.add_entity("quality_check", check)
                checks.append(check_id)
        self.state["lots"][lot_id] = {"lot_id": lot_id, "order_id": order["order_id"], "level": order["level"], "lane": order["lane"],
            "unit_ids": output_ids, "produced_run_id": identifier, "status": "pending_qc", "version": 1}
        updated = self.update("run", identifier, {"observed_at": self.now(), "quality_check_ids": checks, "output_unit_ids": output_ids, "output_lot_id": lot_id})
        result = {"run": updated, "checks": [self.owned("quality_check", i) for i in checks], "units": [self.owned("unit", i) for i in output_ids]}
        self.event("run.observed", identifier, run["cell_id"], result, environment=True)
        return result

    def finalize(self, args):
        run = self.owned("run", args["run_id"])
        require(run["run_status"] == "running" and run["observed_at"] is not None, "MANDATORY_OBSERVATIONS_REQUIRED")
        checks = [self.owned("quality_check", i) for i in run["quality_check_ids"]]
        metrics = {m["metric_name"] for m in run["execution_snapshot"]["profile"]["metric_definitions"]}
        require(len(checks) == len(run["output_unit_ids"]) * len(metrics), "INCOMPLETE_QC_COVERAGE")
        accepted, quarantined = [], []
        for uid in run["output_unit_ids"]:
            rows = [c for c in checks if c["unit_id"] == uid]
            require(len(rows) == len(metrics) and {c["metric_name"] for c in rows} == metrics and all(c["run_id"] == run["run_id"] for c in rows), "QC_UNIT_RUN_IDENTITY")
            (accepted if all(c["expected_min"] <= c["measured_value"] <= c["expected_max"] for c in rows) else quarantined).append(uid)
        require(set(args["accepted_unit_ids"]) == set(accepted) and set(args["quarantined_unit_ids"]) == set(quarantined), "UNIT_DISPOSITION_MISMATCH")
        identifier = self.next_id("disposition")
        record = {"disposition_id": identifier, "run_id": run["run_id"], "lot_id": run["output_lot_id"], "order_id": run["order_id"], "release_id": run["release_id"],
            "quality_check_ids": deepcopy(run["quality_check_ids"]), "accepted_unit_ids": accepted, "quarantined_unit_ids": quarantined,
            "produced_quantity": len(run["output_unit_ids"]), "defective_unit_count": len(quarantined), "completed_at": self.now()}
        self.state["dispositions"][identifier] = record
        for uid in accepted + quarantined:
            self.state["units"][uid].update(status="available" if uid in accepted else "quarantined", quality_accepted=uid in accepted)
        self.state["lots"][run["output_lot_id"]]["status"] = "accepted" if not quarantined else "quarantined" if not accepted else "partial"
        self.update("run", run["run_id"], {"run_status": "completed", "ended_at": self.now(), "cycle_count": len(run["output_unit_ids"]), "defect_count": len(quarantined), "disposition_id": identifier})
        self.update("fixture", run["fixture_id"], {"occupancy_state": "empty"})
        self.event("run.disposition", identifier, run["cell_id"], record)
        return deepcopy(record)

    def poll_updates(self):
        if self.state["applied_updates"]:
            return {"status": "drained", "events": []}
        roots = [o for o in self.state["orders"].values() if o["level"] == 0]
        if not all(len(self.available_output(o["order_id"])) == o["output_quantity"] for o in roots):
            return {"status": "awaiting_first_layer", "events": []}
        planned = self.state["scheduled_updates"][0]
        tool = self.owned("robot_tool", planned["tool_id"])
        if self.busy(tool["cell_id"]):
            raise BusinessError("CELL_OCCUPIED", self.cell_occupancy(tool["cell_id"]))
        after = self.update("robot_tool", tool["tool_id"], {"revision": tool["revision"] + 1, "calibration_status": "due", "calibration_id": None, "compensation_units": 0})
        self.state["tool_biases"][tool["tool_id"]] = planned["new_bias_units"]
        public_update = {k: v for k, v in planned.items() if k != "new_bias_units"}
        released = {**public_update, "tool_revision": after["revision"], "released_at": self.now()}
        self.state["applied_updates"].append(released)
        self.event("tool.drifted", tool["tool_id"], tool["cell_id"], {"update": released, "before": tool, "after": after}, environment=True)
        return {"status": "update_released", "events": [deepcopy(released)]}

    def handoff(self, args):
        if self.state["dispatch"]:
            require(set(args["unit_ids"]) == set(self.state["dispatch"]["unit_ids"]) and set(args["disposition_ids"]) == set(self.state["dispatch"]["disposition_ids"]), "DISPATCH_ALREADY_FROZEN")
            return deepcopy(self.state["dispatch"])
        require(len(self.state["applied_updates"]) == len(self.state["scheduled_updates"]), "DECLARED_UPDATE_INCOMPLETE")
        require(not any(r["run_status"] in {"queued", "running"} for r in self.world["tables"]["run"]), "ACTIVE_RUNS_REMAIN")
        selected = [self.owned("unit", i) for i in args["unit_ids"]]
        require(all(u["order_id"] in self.public["terminal_order_ids"] and u["quality_accepted"] and u["status"] == "available" for u in selected), "TERMINAL_UNIT_NOT_AVAILABLE")
        for order_id in self.public["terminal_order_ids"]:
            order = self.owned("order", order_id)
            rows = [u for u in selected if u["order_id"] == order_id]
            require(len(rows) == order["output_quantity"] and {u["output_slot"] for u in rows} == set(range(order["output_quantity"])), "EXACT_TERMINAL_QUANTITY_AND_SLOT_JOIN")
        expected = {self.owned("run", u["produced_run_id"])["disposition_id"] for u in selected}
        require(set(args["disposition_ids"]) == expected, "DISPOSITION_JOIN_MISMATCH")
        selected_ids = set(args["unit_ids"])
        require(all(u["unit_id"] in selected_ids or u["status"] == "consumed" for u in self.state["units"].values()), "MATERIAL_BALANCE_UNCLOSED")
        record = {"unit_ids": sorted(args["unit_ids"]), "disposition_ids": sorted(args["disposition_ids"]), "dispatched_at": self.now(),
            "terminal_quantities": {i: self.state["orders"][i]["output_quantity"] for i in self.public["terminal_order_ids"]}}
        self.state["dispatch"] = record
        for uid in selected_ids:
            self.state["units"][uid]["status"] = "dispatched"
        self.event("line.dispatched", self.world["task_id"], self.public["cell_ids"][0], record)
        return deepcopy(record)

    def validate_state(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows) and all(r.get("task_id") == self.world["task_id"] for r in rows), "ENTITY_IDENTITY")
        for lot in self.state["lots"].values():
            require(len(lot["unit_ids"]) == len(set(lot["unit_ids"])) and all(self.state["units"][i]["lot_id"] == lot["lot_id"] for i in lot["unit_ids"]), "LOT_UNIT_IDENTITY")
        consumed_groups = {}
        for unit in self.state["units"].values():
            require(not(unit["reserved_by"] and unit["consumed_by"]), "MATERIAL_RESERVATION_AND_CONSUMPTION")
            if unit["consumed_by"]:
                rid = unit["consumed_by"]
                if rid not in consumed_groups:
                    run = self.owned("run", rid)
                    # Count group membership exactly as the previous per-unit
                    # scan; only avoid copying the same full run for every unit.
                    consumed_groups[rid] = Counter(uid for group in run["input_groups"] for uid in set(group))
                require(consumed_groups[rid][unit["unit_id"]] == 1, "MATERIAL_CONSUMPTION_IDENTITY")

    def record_call(self, name, arguments, result):
        pass

    def state_snapshot(self):
        return deepcopy(self.world)

    def persist_world(self):
        write_json(self.runtime / "world.json", self.world)

    def call(self, name, arguments):
        before = self.state_snapshot()
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.validate_state()
            result = {"status": "success", "data": data, "message": ""}
        except BusinessError as error:
            self.world = before
            self.sm.bind(self.world)
            result = {"status": "error", "data": deepcopy(error.details), "message": str(error)}
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        self.record_call(name, arguments, result)
        self.world["sequence"] += 1
        try:
            self.persist_world()
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        return result
