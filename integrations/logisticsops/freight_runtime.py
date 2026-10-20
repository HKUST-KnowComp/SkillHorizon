"""Custody chains are actual shared freight plans, not implied by old bag releases."""

from copy import deepcopy
from datetime import timedelta

from .kernel import require, schema
from .runtime import time
from .network_runtime import TransferNetwork
from .freight_build import FORMAT


class FreightNetwork(TransferNetwork):
    expected_format = FORMAT

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 64}
        definitions = {
            "schedule_baggage_forwarding": ("Schedule actual bags across one airport edge, consuming shared service weight capacity and joining their precise incoming custody plans.",
                schema({"service_id": text, "baggage_release_ids": {**ids, "minItems": 1}, "prior_forwarding_ids": ids,
                        "weight_kg": {"type": "integer", "minimum": 1}})),
            "read_baggage_forwarding": ("Read one historical freight manifest, including actual bag identities, source plans and timing.", schema({"forwarding_id": text})),
            "cancel_baggage_forwarding": ("Cancel an unexecuted synthetic freight plan; retain its record and invalidate dependent custody without automatically repairing it.", schema({"forwarding_id": text})),
        }
        self.metadata.update({name: {"description": d, "input_schema": s} for name, (d, s) in definitions.items()})
        inputs = self.metadata["schedule_airport_transfer"]["input_schema"]
        inputs["properties"]["baggage_forwarding_ids"] = ids
        inputs["required"].append("baggage_forwarding_ids")

    def service(self, identifier):
        rows = [s for s in self.public["transfer_network"]["freight_services"] if s["service_id"] == identifier]
        require(len(rows) == 1, "FREIGHT_SERVICE_NOT_FOUND")
        return rows[0]

    def service_available(self, identifier):
        spec = self.public["transfer_network"].get("service_disruption")
        if not spec:
            return True
        occurred = bool(self.network["service_events"])
        if identifier == spec["withdrawn_service_id"]:
            return not occurred
        if identifier == spec["activated_service_id"]:
            return occurred
        return True

    def current_forwarding(self, identifier):
        return self.checked("forwarding", identifier, lambda: self._current_forwarding(identifier))

    def _current_forwarding(self, identifier):
        require(identifier in self.network["forwardings"], "FREIGHT_PLAN_NOT_FOUND")
        row = self.network["forwardings"][identifier]
        require(row["status"] == "scheduled", "FREIGHT_PLAN_CANCELLED")
        require(self.service_available(row["service_id"]), "FREIGHT_SERVICE_UNAVAILABLE")
        for release in row["baggage_release_ids"]:
            self.current_release(release)
        for parent in row["prior_forwarding_ids"]:
            self.current_forwarding(parent)
        return row

    def custody(self, release_ids, forwarding_ids, hub):
        releases = [self.current_release(i) for i in release_ids]
        incoming = [self.current_forwarding(i) for i in forwarding_ids]
        required, ready = set(), []
        for release in releases:
            require(release["origin_leg"] <= hub, "FREIGHT_ORIGIN_AFTER_HUB")
            if release["origin_leg"] == hub:
                origin = self.current_clearance(release["origin_clearance_id"])
                flight = self.owned("flight", origin["snapshot"]["booking"]["flight_id"])
                ready.append(time(flight["scheduled_arrival"]) + timedelta(minutes=release["handling_minutes"]))
            else:
                matches = [r for r in incoming if release["release_id"] in r["baggage_release_ids"] and r["to_hub"] == hub]
                require(len(matches) == 1, "EXACT_FREIGHT_CUSTODY_REQUIRED")
                row = matches[0]
                required.add(row["forwarding_id"])
                ready.append(time(row["arrival_time"]) + timedelta(minutes=self.service(row["service_id"])["handover_minutes"]))
        require(required == set(forwarding_ids), "UNRELATED_FREIGHT_CUSTODY")
        return releases, ready

    def _current_trip(self, identifier, seen=None):
        row = super()._current_trip(identifier, seen)
        self.custody(row["baggage_release_ids"], row["baggage_forwarding_ids"], row["leg"] - 1)
        return row

    def schedule(self, args):
        group = next((g for g in self.public["transfer_network"]["groups"] if g["group_id"] == args["group_id"]), None)
        require(group is not None, "GROUND_GROUP_OR_VEHICLE_NOT_FOUND")
        _, ready = self.custody(args["baggage_release_ids"], args["baggage_forwarding_ids"], group["leg"] - 1)
        try:
            departure = time(args["departure_time"])
            require(departure.tzinfo is not None, "GROUND_TIMEZONE_REQUIRED")
        except (TypeError, ValueError):
            require(False, "GROUND_TIME_FORMAT")
        if ready:
            require(departure >= max(ready), "GROUND_BEFORE_ACTUAL_CARGO_ARRIVAL", {
                "violations": ["departure_before_freight_readiness"],
                "earliest_departure_from_freight": max(ready).isoformat(),
                "supplied_departure_time": departure.isoformat(),
            })
        result = super().schedule(args)
        event = self.public["transfer_network"].get("service_disruption")
        if event and not self.network["service_events"] and group["leg"] == event["trigger_ground_leg"]:
            bulletin = {"event_id": event["event_id"], "trigger_trip_id": result["trip_id"],
                "withdrawn_service_id": event["withdrawn_service_id"], "activated_service_id": event["activated_service_id"],
                "issued_at": self._now()}
            self.network["service_events"].append(bulletin)
            result["operations_bulletin"] = deepcopy(bulletin)
        return result

    def dispatch(self, name, args):
        if name == "read_transfer_network":
            result = super().dispatch(name, args)
            result["forwardings"] = [{k: row[k] for k in ("forwarding_id", "service_id", "from_hub", "to_hub", "status")}
                                     for row in self.network["forwardings"].values()]
            if "service_disruption" in self.public["transfer_network"]:
                result["service_events"] = deepcopy(self.network["service_events"])
            services = (result["freight_services"] if args.get("include_static", True)
                        else [{"service_id": s["service_id"]} for s in self.public["transfer_network"]["freight_services"]])
            for service in services:
                service["availability"] = "available" if self.service_available(service["service_id"]) else "unavailable"
            if not args.get("include_static", True):
                result["service_availability"] = services
            return result
        if name in {"read_baggage_forwarding", "cancel_baggage_forwarding"}:
            require(args["forwarding_id"] in self.network["forwardings"], "FREIGHT_PLAN_NOT_FOUND")
            row = self.network["forwardings"][args["forwarding_id"]]
            if name == "cancel_baggage_forwarding":
                row.update(status="cancelled", cancelled_at=self._now())
            return deepcopy(row)
        if name == "schedule_baggage_forwarding":
            service = self.service(args["service_id"])
            require(self.service_available(service["service_id"]), "FREIGHT_SERVICE_UNAVAILABLE")
            releases, ready = self.custody(args["baggage_release_ids"], args["prior_forwarding_ids"], service["from_hub"])
            if "handling_policy" in self.public["transfer_network"]:
                obligations = {o["obligation_id"]: o for o in self.public["transfer_network"]["baggage_obligations"]}
                require(all(obligations[r["obligation_id"]]["required_handling"] in service["handling_capabilities"]
                            for r in releases), "FREIGHT_HANDLING_INELIGIBLE")
            require(all(r["delivery_leg"] > service["to_hub"] for r in releases), "BAG_ALREADY_DUE_AT_SOURCE_HUB")
            weight = sum(r["bag"]["weight_kg"] for r in releases)
            require(weight == args["weight_kg"], "FREIGHT_ACTUAL_WEIGHT")
            active = [r for r in self.network["forwardings"].values() if r["status"] == "scheduled"]
            physical_bags = {r["bag_id"] for r in releases}
            require(len(physical_bags) == len(releases), "DUPLICATE_BAG_HUB_MOVEMENT")
            require(not any(r["from_hub"] == service["from_hub"] and physical_bags.intersection(
                self.network["releases"][i]["bag_id"] for i in r["baggage_release_ids"]) for r in active), "DUPLICATE_BAG_HUB_MOVEMENT")
            used = sum(r["weight_kg"] for r in active if r["service_id"] == service["service_id"])
            require(used + weight <= service["capacity_kg"], "FREIGHT_SHARED_CAPACITY")
            require(max(ready) <= time(service["departure_time"]), "FREIGHT_BEFORE_CUSTODY_READY")
            row = {**deepcopy(args), "forwarding_id": self.new_id("freight-plan"),
                   **{k: service[k] for k in ("from_hub", "to_hub", "departure_time", "arrival_time")},
                   "status": "scheduled", "issued_at": self._now(), **self.attribution_fields()}
            self.network["forwardings"][row["forwarding_id"]] = row
            return deepcopy(row)
        return super().dispatch(name, args)
