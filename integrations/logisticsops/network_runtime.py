"""Operational ground plans consume actual cross-cohort and multi-origin evidence."""

from copy import deepcopy
from datetime import timedelta
import math

from .kernel import require, schema
from .runtime import Connections, time
from .network_build import FORMAT
from .public_contract import GROUND_TIMING


class TransferNetwork(Connections):
    expected_format = FORMAT

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        require(self.public["format"] == self.expected_format and self.world["format"] == self.expected_format, "TRANSFER_NETWORK_FORMAT")
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 64}
        definitions = {
            "read_transfer_network": ("Read current network records. include_static=false omits immutable definitions already returned by read_task, retaining complete current inventories and service availability.", schema({"include_static": {"type": "boolean"}}, [])),
            "release_through_baggage": ("Release an actual source bag from its current cleared origin booking; retain weight, origin and delivery obligation.", schema({"obligation_id": text, "origin_clearance_id": text})),
            "read_baggage_release": ("Read an actual through-baggage release, not a synthesized due manifest.", schema({"release_id": text})),
            "read_airport_transfer": ("Read an actual scheduled vehicle trip and its exact supplied dependencies.", schema({"trip_id": text})),
            "cancel_airport_transfer": ("Cancel a synthetic unexecuted operational plan. Preserve its history; dependent plans/clearances become stale, not silently repaired.", schema({"trip_id": text})),
            "schedule_airport_transfer": ("Schedule one cross-cohort group using actual inbound clearances, due baggage releases and a vehicle's previous trip. Validate supplied physical load and times, not a preferred route. " + GROUND_TIMING, schema({
                "group_id": text, "vehicle_id": text, "inbound_clearance_ids": ids, "baggage_release_ids": ids,
                "previous_vehicle_trip_id": text, "departure_time": text, "arrival_time": text,
                "weight_kg": {"type": "integer", "minimum": 0}, "wheelchair_count": {"type": "integer", "minimum": 0}},
                ["group_id", "vehicle_id", "inbound_clearance_ids", "baggage_release_ids", "departure_time", "arrival_time", "weight_kg", "wheelchair_count"]))}
        self.metadata.update({name: {"description": description, "input_schema": inputs} for name, (description, inputs) in definitions.items()})
        self.metadata["issue_connection_clearance"]["input_schema"]["properties"]["transfer_trip_id"] = text
        self.metadata["issue_connection_clearance"]["description"] += " Later legs also bind the group's actual current ground-transfer trip and arrival."

    @property
    def network(self):
        return self.world["transfer_network"]

    def new_id(self, kind):
        number = self.network["next_id"]
        self.network["next_id"] += 1
        return f"{kind}-{number}"

    def obligation(self, identifier):
        rows = [r for r in self.public["transfer_network"]["baggage_obligations"] if r["obligation_id"] == identifier]
        require(len(rows) == 1, "BAGGAGE_OBLIGATION_NOT_FOUND")
        return rows[0]

    def current_release(self, identifier):
        return self.checked("release", identifier, lambda: self._current_release(identifier))

    def checked(self, kind, identifier, function):
        root = not hasattr(self, "_validation_cache")
        if root:
            self._validation_cache, self._validation_active = {}, set()
        key = (kind, identifier)
        try:
            if key in self._validation_cache:
                return self._validation_cache[key]
            require(key not in self._validation_active, "GROUND_DEPENDENCY_CYCLE")
            self._validation_active.add(key)
            result = function()
            self._validation_active.remove(key)
            self._validation_cache[key] = result
            return result
        finally:
            if root:
                del self._validation_cache, self._validation_active

    def _current_release(self, identifier):
        require(identifier in self.network["releases"], "BAGGAGE_RELEASE_NOT_FOUND")
        row = self.network["releases"][identifier]
        obligation = self.obligation(row["obligation_id"])
        clearance = self.current_clearance(row["origin_clearance_id"])
        bag = self.owned("baggage", obligation["bag_id"])
        require(clearance["booking_id"] == obligation["origin_booking_id"] and row["bag"] == bag, "STALE_OR_WRONG_BAGGAGE_RELEASE")
        return row

    def current_trip(self, identifier, seen=None):
        return self.checked("trip", identifier, lambda: self._current_trip(identifier, seen))

    def _current_trip(self, identifier, seen=None):
        seen = set() if seen is None else seen
        require(identifier not in seen, "GROUND_TRIP_CYCLE")
        seen.add(identifier)
        require(identifier in self.network["trips"], "GROUND_TRIP_NOT_FOUND")
        row = self.network["trips"][identifier]
        require(row["status"] == "scheduled", "GROUND_TRIP_CANCELLED")
        require(all(self.current_clearance(i) for i in row["inbound_clearance_ids"]), "STALE_INBOUND_CLEARANCE")
        require(all(self.current_release(i) for i in row["baggage_release_ids"]), "STALE_BAGGAGE_RELEASE")
        if row.get("previous_vehicle_trip_id"):
            self.current_trip(row["previous_vehicle_trip_id"], seen)
        return row

    def current_clearance(self, identifier, seen=None):
        return self.checked("clearance", identifier, lambda: self._current_clearance(identifier, seen))

    def _current_clearance(self, identifier, seen=None):
        row = super().current_clearance(identifier, seen)
        if row["leg"] > 1:
            require(row.get("transfer_trip_id"), "GROUND_TRANSFER_REQUIRED")
            self.current_trip(row["transfer_trip_id"])
        return row

    def issue_connection_clearance(self, booking_id, previous_clearance_id="", transfer_trip_id=""):
        request = next((r for r in self.public["requests"] if booking_id in r["bookings"]), None)
        require(request is not None, "BOOKING_NOT_IN_TRAVEL_REQUEST")
        leg = request["bookings"].index(booking_id) + 1
        if leg > 1:
            trip = self.current_trip(transfer_trip_id)
            group = next(g for g in self.public["transfer_network"]["groups"] if g["group_id"] == trip["group_id"])
            require(group["leg"] == leg and request["passenger_id"] in group["passenger_ids"] and previous_clearance_id in trip["inbound_clearance_ids"], "WRONG_GROUND_TRANSFER_JOIN")
            booking = self.owned("booking", booking_id)
            flight = self.owned("flight", booking["flight_id"])
            require(time(trip["arrival_time"]) + timedelta(minutes=group["boarding_buffer_minutes"]) <= time(flight["scheduled_departure"]), "GROUND_TRANSFER_MISSES_BOARDING")
        else:
            require(not transfer_trip_id, "INITIAL_LEG_HAS_NO_GROUND_TRANSFER")
        row = super().issue_connection_clearance(booking_id, previous_clearance_id)
        snapshot = row["snapshot"]
        for prior in reversed(list(self.state["clearances"].values())):
            if prior["snapshot"] == snapshot and prior["previous_clearance_id"] == previous_clearance_id and prior.get("transfer_trip_id", "") == transfer_trip_id:
                self.current_clearance(prior["clearance_id"])
                return deepcopy(prior)
        existing = self.state["clearances"][row["clearance_id"]]
        if existing.get("transfer_trip_id", "") not in ("", transfer_trip_id):
            # Preserve the old operational plan when another valid trip replaces it.
            identifier = f"connection-clearance-{self.state['next_clearance']}"
            self.state["next_clearance"] += 1
            existing = {**deepcopy(row), "clearance_id": identifier, "issued_at": self._now()}
            self.state["clearances"][identifier] = existing
        existing["transfer_trip_id"] = transfer_trip_id
        return deepcopy(existing)

    def dispatch(self, name, args):
        if name == "read_transfer_network":
            definitions = deepcopy(self.public["transfer_network"]) if args.get("include_static", True) else {}
            return {**definitions, "releases": [{k: r[k] for k in ("release_id", "obligation_id", "origin_leg", "delivery_leg")} for r in self.network["releases"].values()],
                "trips": [{k: r[k] for k in ("trip_id", "group_id", "vehicle_id", "departure_time", "arrival_time", "status")} for r in self.network["trips"].values()]}
        if name == "release_through_baggage":
            obligation = self.obligation(args["obligation_id"])
            clearance = self.current_clearance(args["origin_clearance_id"])
            require(clearance["booking_id"] == obligation["origin_booking_id"], "WRONG_BAGGAGE_ORIGIN_CLEARANCE")
            bag = self.owned("baggage", obligation["bag_id"])
            require(bag["passenger_id"] == obligation["passenger_id"] and bag["booking_id"] == clearance["booking_id"] and bag["booking_revision"] == clearance["snapshot"]["booking"]["revision"], "BAGGAGE_ORIGIN_BINDING")
            for row in self.network["releases"].values():
                if row["obligation_id"] == obligation["obligation_id"] and row["origin_clearance_id"] == args["origin_clearance_id"] and row["bag"] == bag:
                    return deepcopy(row)
            row = {**deepcopy(obligation), "release_id": self.new_id("cargo-release"), "origin_clearance_id": args["origin_clearance_id"],
                "bag": bag, "issued_at": self._now(), **self.attribution_fields()}
            self.network["releases"][row["release_id"]] = row
            return deepcopy(row)
        if name in {"read_baggage_release", "read_airport_transfer"}:
            key, collection = ("release_id", "releases") if name == "read_baggage_release" else ("trip_id", "trips")
            require(args[key] in self.network[collection], "GROUND_RECORD_NOT_FOUND")
            return deepcopy(self.network[collection][args[key]])
        if name == "cancel_airport_transfer":
            require(args["trip_id"] in self.network["trips"], "GROUND_TRIP_NOT_FOUND")
            row = self.network["trips"][args["trip_id"]]
            row["status"] = "cancelled"
            row["cancelled_at"] = self._now()
            return deepcopy(row)
        if name == "schedule_airport_transfer":
            return self.schedule(args)
        return super().dispatch(name, args)

    def schedule(self, args):
        groups = [g for g in self.public["transfer_network"]["groups"] if g["group_id"] == args["group_id"]]
        vehicles = [v for v in self.public["transfer_network"]["vehicles"] if v["vehicle_id"] == args["vehicle_id"]]
        require(len(groups) == len(vehicles) == 1, "GROUND_GROUP_OR_VEHICLE_NOT_FOUND")
        group, vehicle = groups[0], vehicles[0]
        require(not any(r["group_id"] == group["group_id"] and r["status"] == "scheduled" for r in self.network["trips"].values()), "GROUND_GROUP_ALREADY_SCHEDULED")
        require(vehicle["leg"] == group["leg"], "VEHICLE_WRONG_AIRPORT_STAGE")
        inbound = [self.current_clearance(i) for i in args["inbound_clearance_ids"]]
        require(len(inbound) == len(group["passenger_ids"]) and {c["passenger_id"] for c in inbound} == set(group["passenger_ids"])
            and all(c["leg"] == group["leg"] - 1 for c in inbound), "EXACT_CROSS_COHORT_INBOUND_JOIN")
        due = {o["obligation_id"] for o in self.public["transfer_network"]["baggage_obligations"] if o["delivery_leg"] == group["leg"] and o["passenger_id"] in group["passenger_ids"]}
        cargo = [self.current_release(i) for i in args["baggage_release_ids"]]
        require(len(cargo) == len(due) and {r["obligation_id"] for r in cargo} == due, "EXACT_MULTI_ORIGIN_CARGO_JOIN")
        weight = sum(r["bag"]["weight_kg"] for r in cargo)
        wheelchairs = sum("wheelchair" in next(r for r in self.public["requests"] if r["passenger_id"] == p)["required_ground_services"] for p in group["passenger_ids"])
        require(args["weight_kg"] == weight and args["wheelchair_count"] == wheelchairs, "ACTUAL_GROUND_LOAD")
        require(len(inbound) <= vehicle["passenger_capacity"] and weight <= vehicle["weight_capacity_kg"] and wheelchairs <= vehicle["wheelchair_capacity"], "GROUND_VEHICLE_CAPACITY")
        arrivals = [time(self.owned("flight", c["snapshot"]["booking"]["flight_id"])["scheduled_arrival"]) for c in inbound]
        for release in cargo:
            origin = self.current_clearance(release["origin_clearance_id"])
            arrivals.append(time(self.owned("flight", origin["snapshot"]["booking"]["flight_id"])["scheduled_arrival"]) + timedelta(minutes=release["handling_minutes"]))
        previous = [r for r in self.network["trips"].values() if r["vehicle_id"] == vehicle["vehicle_id"] and r["status"] == "scheduled"]
        if previous:
            latest = previous[-1]
            require(args.get("previous_vehicle_trip_id") == latest["trip_id"], "ACTUAL_PREVIOUS_VEHICLE_TRIP_REQUIRED")
            self.current_trip(latest["trip_id"])
            arrivals.append(time(latest["arrival_time"]) + timedelta(minutes=vehicle["turnaround_minutes"]))
        else:
            require(not args.get("previous_vehicle_trip_id"), "NO_PREVIOUS_VEHICLE_TRIP")
        try:
            departure, arrival = time(args["departure_time"]), time(args["arrival_time"])
            require(departure.tzinfo is not None and arrival.tzinfo is not None, "GROUND_TIMEZONE_REQUIRED")
        except (TypeError, ValueError):
            require(False, "GROUND_TIME_FORMAT")
        duration = vehicle["base_travel_minutes"] + math.ceil(weight / vehicle["weight_step_kg"]) * vehicle["minutes_per_weight_step"] + wheelchairs * vehicle["wheelchair_minutes"]
        earliest = max(arrivals)
        expected_arrival = departure + timedelta(minutes=duration)
        violations = []
        if departure < earliest:
            violations.append("departure_before_readiness")
        if arrival != expected_arrival:
            violations.append("arrival_duration_mismatch")
        require(not violations, "ACTUAL_GROUND_TIMING", {
            "violations": violations,
            "earliest_departure_from_inbound_baggage_and_vehicle": earliest.isoformat(),
            "supplied_departure_time": departure.isoformat(),
            "supplied_arrival_time": arrival.isoformat(),
            "expected_duration_minutes": duration,
            "expected_arrival_for_supplied_departure": expected_arrival.isoformat(),
        })
        row = {**deepcopy(args), "trip_id": self.new_id("ground-trip"), "leg": group["leg"], "duration_minutes": duration, "status": "scheduled",
            "issued_at": self._now(), **self.attribution_fields()}
        self.network["trips"][row["trip_id"]] = row
        return deepcopy(row)
