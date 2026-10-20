"""Connected travel handoffs plus observational skill execution, without a router."""

from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path
import subprocess
import sys
import tempfile

from integrations.internal_mcp.io import read_json, controller_source
from .kernel import Airline, cents, require, schema
from .planning_contract import PLANNING_INPUT_SCHEMA


def time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Connections(Airline):
    def __init__(self, task, runtime):
        self.task = Path(task).resolve()
        self.public = read_json(self.task / "public/task.json")
        source = controller_source(self.task)
        super().__init__(source, runtime)
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        text = {"type": "string", "minLength": 1}
        strings = {"type": "array", "items": text, "maxItems": 64, "uniqueItems": True}
        definitions = {
            "read_task": ("Read public travel obligations, route catalog, spend limit and reusable skill library.", schema({})),
            "read_skill_file": ("Read a public skill instruction or its optional source script.", schema({"skill_id": text, "path": {"type": "string", "default": "SKILL.md"}}, ["skill_id"])),
            "begin_skill": ("Record the workflow selected by the agent. This is observational, not a gold router or business permission.",
                schema({"skill_id": text, "booking_ids": strings}, ["skill_id"])),
            "select_skill_run": ("Select an existing open skill run for attributing subsequent work.", schema({"run_id": text})),
            "finish_skill": ("Record claimed completion and output references. Does not evaluate travel success.",
                schema({"run_id": text, "output_ids": strings, "notes": {"type": "string", "maxLength": 4000}}, ["run_id"])),
            "read_skill_run": ("Read an observed skill run and its tool-call outcomes.", schema({"run_id": text})),
            "list_clearances": ("List created travel clearances in stable creation order, including stale history.",
                schema({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, [])),
            "read_clearance": ("Read a created airline clearance record. This does not repair or certify its continued validity.", schema({"clearance_id": text})),
            "issue_connection_clearance": ("Issue a predeparture service handoff from the current booked itinerary and actual previous-leg clearance.",
                schema({"booking_id": text, "previous_clearance_id": {"type": "string"}}, ["booking_id"])),
            "run_skill_script": ("Execute the optional planning script with supplied public JSON only; no environment or evaluator access.",
                schema({"skill_id": text, "inputs": deepcopy(PLANNING_INPUT_SCHEMA)})),
        }
        self.metadata.update({name: {"description": description, "input_schema": inputs} for name, (description, inputs) in definitions.items()})

    @property
    def state(self):
        return self.world["connection_state"]

    def attribution_fields(self):
        return {"produced_by_skill_run": self.state["active_skill_run"]}

    def skill(self, identifier):
        matches = [s for s in self.public["skills"] if s["name"] == identifier]
        require(len(matches) == 1, "UNKNOWN_SKILL")
        return matches[0]

    def snapshot(self, booking_id):
        booking = self.owned("booking", booking_id)
        services = {}
        for kind, pk in (("baggage", "baggage_id"), ("special_service", "service_id")):
            services[kind] = [{key: row.get(key) for key in (pk, "passenger_id", "flight_id", "booking_revision", "status")}
                for row in self.world["tables"][kind] if row.get("booking_id") == booking_id]
            services[kind].sort(key=lambda row: row[pk])
        return {"booking": {key: booking.get(key) for key in ("booking_id", "passenger_id", "flight_id", "booking_class", "revision", "seat_number", "status", "paid_amount", "ticket_price", "change_fee")},
                "payment_ids": sorted(p["payment_id"] for p in self.world["tables"]["payment"] if p.get("booking_id") == booking_id and p["status"] == "completed"),
                **services}

    def current_clearance(self, identifier, seen=None):
        seen = set() if seen is None else seen
        require(identifier not in seen, "CLEARANCE_CYCLE")
        seen.add(identifier)
        require(identifier in self.state["clearances"], "CLEARANCE_NOT_FOUND")
        row = self.state["clearances"][identifier]
        require(row["snapshot"] == self.snapshot(row["booking_id"]), "STALE_CLEARANCE")
        if row["previous_clearance_id"]:
            self.current_clearance(row["previous_clearance_id"], seen)
        return row

    def issue_connection_clearance(self, booking_id, previous_clearance_id=""):
        requests = [r for r in self.public["requests"] if booking_id in r["bookings"]]
        require(len(requests) == 1, "BOOKING_NOT_IN_TRAVEL_REQUEST")
        request = requests[0]
        leg = request["bookings"].index(booking_id)
        booking = self.owned("booking", booking_id)
        require(booking["status"] == "checked_in", "BOOKING_NOT_CHECKED_IN")
        flight = self.owned("flight", booking["flight_id"])
        require(flight["status"] in {"scheduled", "boarding"}, "FLIGHT_NOT_ELIGIBLE")
        require(booking["passenger_id"] == request["passenger_id"], "PASSENGER_IDENTITY_MISMATCH")
        if leg:
            require(previous_clearance_id, "PREVIOUS_CLEARANCE_REQUIRED")
            previous = self.current_clearance(previous_clearance_id)
            require(previous["booking_id"] == request["bookings"][leg - 1], "WRONG_PREVIOUS_LEG")
            prev_flight = self.owned("flight", previous["snapshot"]["booking"]["flight_id"])
            gap = (time(flight["scheduled_departure"]) - time(prev_flight["scheduled_arrival"])).total_seconds() / 60
            require(prev_flight["arrival_airport"] == flight["departure_airport"] and gap >= request["minimum_connection_minutes"], "CONNECTION_NOT_FEASIBLE")
        else:
            require(not previous_clearance_id, "UNEXPECTED_PREDECESSOR")
        if leg == len(request["bookings"]) - 1:
            require(time(flight["scheduled_arrival"]) <= time(request["final_deadline"]), "FINAL_DEADLINE_MISSED")
        # A service-desk approval checks the actual shared spend, not the reference portfolio.
        net = sum((1 if p["direction"] == "charge" else -1) * cents(p["amount"])
                  for p in self.world["tables"]["payment"] if p.get("task_id") == self.world["task_id"] and p["status"] == "completed")
        require(net <= cents(self.public["max_net_spend_usd"]), "TRAVEL_SPEND_LIMIT")
        snapshot = self.snapshot(booking_id)
        for row in self.state["clearances"].values():
            if row["snapshot"] == snapshot and row["previous_clearance_id"] == previous_clearance_id:
                return deepcopy(row)
        identifier = f"connection-clearance-{self.state['next_clearance']}"
        self.state["next_clearance"] += 1
        row = {"clearance_id": identifier, "booking_id": booking_id, "passenger_id": request["passenger_id"],
               "leg": leg + 1, "snapshot": snapshot, "previous_clearance_id": previous_clearance_id, "issued_at": self._now(),
               **self.attribution_fields()}
        self.state["clearances"][identifier] = row
        return deepcopy(row)

    def dispatch(self, name, args):
        if name == "read_task":
            return deepcopy(self.public)
        if name == "read_skill_file":
            spec = self.skill(args["skill_id"])
            path = args.get("path", "SKILL.md")
            require(path in spec["files"], "SKILL_FILE_NOT_EXPOSED")
            return {"skill_id": spec["name"], "path": path, "content": (self.task / "public/skills" / spec["name"] / path).read_text()}
        if name == "begin_skill":
            self.skill(args["skill_id"])
            for identifier in args.get("booking_ids", []):
                self.owned("booking", identifier)
            identifier = f"skill-run-{self.state['next_skill_run']}"
            self.state["next_skill_run"] += 1
            self.state["skill_runs"][identifier] = {"run_id": identifier, "skill_id": args["skill_id"],
                "booking_ids": args.get("booking_ids", []), "status": "open", "calls": [], "output_ids": [], "notes": ""}
            self.state["active_skill_run"] = identifier
            return deepcopy(self.state["skill_runs"][identifier])
        if name in {"select_skill_run", "finish_skill", "read_skill_run"}:
            require(args["run_id"] in self.state["skill_runs"], "SKILL_RUN_NOT_FOUND")
            row = self.state["skill_runs"][args["run_id"]]
            if name != "read_skill_run":
                require(row["status"] == "open", "SKILL_RUN_CLOSED")
            if name == "select_skill_run":
                self.state["active_skill_run"] = row["run_id"]
            if name == "finish_skill":
                row.update({"status": "claimed_complete", "output_ids": args.get("output_ids", []), "notes": args.get("notes", "")})
                if self.state["active_skill_run"] == row["run_id"]:
                    self.state["active_skill_run"] = None
            return deepcopy(row)
        if name == "list_clearances":
            rows = list(self.state["clearances"].values())
            offset, limit = args.get("offset", 0), args.get("limit", 20)
            return {"total": len(rows), "offset": offset, "records": deepcopy(rows[offset:offset + limit])}
        if name == "read_clearance":
            require(args["clearance_id"] in self.state["clearances"], "CLEARANCE_NOT_FOUND")
            return deepcopy(self.state["clearances"][args["clearance_id"]])
        if name == "issue_connection_clearance":
            return self.issue_connection_clearance(**args)
        if name == "run_skill_script":
            require(args["skill_id"] == "plan-reaccommodation", "SKILL_HAS_NO_SCRIPT")
            payload = json.dumps(args["inputs"], allow_nan=False)
            require(len(payload.encode()) <= 131072, "SCRIPT_INPUT_TOO_LARGE")
            path = self.task / "public/skills/plan-reaccommodation/scripts/check_connections.py"
            with tempfile.TemporaryDirectory(prefix="c09-plan-") as directory:
                result = subprocess.run([sys.executable, "-I", "-S", str(path)], input=payload, text=True,
                    capture_output=True, cwd=directory, env={"PYTHONIOENCODING": "utf-8"}, timeout=5)
            require(result.returncode == 0, "SCRIPT_INPUT_OR_EXECUTION_ERROR")
            output = json.loads(result.stdout)
            require("input_error" not in output, "SCRIPT_INPUT_ERROR", output.get("input_error"))
            return output
        return super().dispatch(name, args)

    def record_call(self, name, arguments, result):
        active = self.state["active_skill_run"]
        if active and name not in {"begin_skill", "select_skill_run", "read_skill_run"}:
            self.state["skill_runs"][active]["calls"].append({"sequence": self.world["sequence"], "tool": name,
                "status": result["status"], "booking_id": arguments.get("booking_id") if isinstance(arguments, dict) else None,
                "error": result["message"] if result["status"] != "success" else ""})
