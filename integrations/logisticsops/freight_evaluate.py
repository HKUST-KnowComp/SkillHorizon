"""Independent cargo identity, conservation, location and timetable verification."""

from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path

from integrations.internal_mcp.io import read_json
from .network_evaluate import _evaluate as evaluate_network, at


FORMAT = "skillhorizon.c09.freight_network.v2"
EVALUATION_REVISION = "physical_custody_v3"


def _evaluate(task, runtime):
    task, runtime = Path(task), Path(runtime)
    public = read_json(task / "private/initial_obligations.json")["public"]
    initial, world = read_json(task / "checkpoint/world.json"), read_json(runtime / "world.json")
    spec, state = public["transfer_network"], world["transfer_network"]
    services = {r["service_id"]: r for r in spec["freight_services"]}
    obligations = {r["obligation_id"]: r for r in spec["baggage_obligations"]}
    bags = {r["baggage_id"]: r for r in initial["tables"]["baggage"]}
    flights = {r["flight_id"]: r for r in initial["tables"]["flight"]}
    releases, manifests = state["releases"], state["forwardings"]
    base = evaluate_network(task, runtime, expected_format=FORMAT,
        active_freight_release_ids=[i for row in manifests.values() if row["status"] == "scheduled"
                                   for i in row["baggage_release_ids"]])
    clearances = world["connection_state"]["clearances"]
    errors, active, verified, depth = [], set(), {}, {}
    use, movements, origin_joins = Counter(), Counter(), []

    def check(condition, code):
        if not condition:
            errors.append(code)
        return condition

    disruption = spec.get("service_disruption")
    events = state.get("service_events", [])
    if disruption:
        trigger_trips = [r for r in state["trips"].values() if r["leg"] == disruption["trigger_ground_leg"]]
        check(bool(trigger_trips) and len(events) == 1, "FREIGHT_OPERATIONS_EVENT_REQUIRED")
        if events:
            event = events[0]
            check(all(event[k] == disruption[k] for k in ("event_id", "withdrawn_service_id", "activated_service_id"))
                  and bool(trigger_trips) and event["trigger_trip_id"] == trigger_trips[0]["trip_id"]
                  and event["issued_at"] == trigger_trips[0]["issued_at"], "FREIGHT_FROZEN_OPERATIONS_EVENT")

    def custody(release_ids, parent_ids, hub):
        check(len(release_ids) == len(set(release_ids)) and len(parent_ids) == len(set(parent_ids)), "FREIGHT_DUPLICATE_INPUT")
        parents = [manifests[i] for i in parent_ids]
        required, ready = set(), []
        for identifier in release_ids:
            release = releases[identifier]
            obligation = obligations[release["obligation_id"]]
            check(all(release[k] == v for k, v in obligation.items()), "FREIGHT_FROZEN_RELEASE_IDENTITY")
            origin = clearances[release["origin_clearance_id"]]
            check(origin["booking_id"] == obligation["origin_booking_id"] and origin["leg"] == obligation["origin_leg"], "FREIGHT_ACTUAL_ORIGIN_CLEARANCE")
            check(obligation["origin_leg"] <= hub, "FREIGHT_WRONG_ORIGIN_HUB")
            if obligation["origin_leg"] == hub:
                flight = flights[origin["snapshot"]["booking"]["flight_id"]]
                ready.append(at(flight["scheduled_arrival"]) + timedelta(minutes=obligation["handling_minutes"]))
            else:
                matches = [r for r in parents if identifier in r["baggage_release_ids"] and r["to_hub"] == hub]
                if not check(len(matches) == 1, "FREIGHT_MISSING_OR_WRONG_CUSTODY"):
                    continue
                parent = matches[0]
                required.add(parent["forwarding_id"])
                check(verify(parent["forwarding_id"]), "FREIGHT_STALE_PREDECESSOR")
                ready.append(at(parent["arrival_time"]) + timedelta(minutes=services[parent["service_id"]]["handover_minutes"]))
        check(required == set(parent_ids), "FREIGHT_UNRELATED_PREDECESSOR")
        return ready

    def verify(identifier):
        if identifier in verified:
            return verified[identifier]
        if identifier in active:
            check(False, "FREIGHT_CUSTODY_CYCLE")
            return False
        active.add(identifier)
        before = len(errors)
        row = manifests[identifier]
        service = services[row["service_id"]]
        if disruption:
            check(row["service_id"] != disruption["withdrawn_service_id"], "FREIGHT_SERVICE_UNAVAILABLE")
            if row["service_id"] == disruption["activated_service_id"]:
                check(bool(events) and at(row["issued_at"]) > at(events[0]["issued_at"]), "FREIGHT_STANDBY_NOT_YET_ACTIVE")
        check(row["forwarding_id"] == identifier and row["status"] == "scheduled", "FREIGHT_INACTIVE_MANIFEST")
        check(all(row[k] == service[k] for k in ("from_hub", "to_hub", "departure_time", "arrival_time"))
              and service["to_hub"] == service["from_hub"] + 1, "FREIGHT_FROZEN_ROUTE_AND_TIMING")
        ready = custody(row["baggage_release_ids"], row["prior_forwarding_ids"], service["from_hub"])
        due = [obligations[releases[i]["obligation_id"]] for i in row["baggage_release_ids"]]
        if "handling_policy" in spec:
            check(all(o["required_handling"] in service["handling_capabilities"] for o in due),
                  "FREIGHT_HANDLING_INELIGIBLE")
        weight = sum(bags[o["bag_id"]]["weight_kg"] for o in due)
        check(bool(due) and all(o["delivery_leg"] > service["to_hub"] for o in due), "FREIGHT_FORWARDING_PAST_DESTINATION")
        check(row["weight_kg"] == weight, "FREIGHT_FROZEN_PHYSICAL_WEIGHT")
        check(bool(ready) and max(ready) <= at(service["departure_time"]), "FREIGHT_ACTUAL_CUSTODY_TIME")
        active.remove(identifier)
        verified[identifier] = len(errors) == before
        depth[identifier] = 1 + max((depth.get(i, 0) for i in row["prior_forwarding_ids"]), default=0)
        return verified[identifier]

    for row in manifests.values():
        if row["status"] != "scheduled":
            continue
        verify(row["forwarding_id"])
        service = services[row["service_id"]]
        weight = sum(bags[obligations[releases[i]["obligation_id"]]["bag_id"]]["weight_kg"] for i in row["baggage_release_ids"])
        use[service["service_id"]] += weight
        movements.update((obligations[releases[i]["obligation_id"]]["bag_id"], service["from_hub"])
                         for i in row["baggage_release_ids"])
        origin_joins.append(len({obligations[releases[i]["obligation_id"]]["origin_leg"] for i in row["baggage_release_ids"]}))
    check(all(value <= services[key]["capacity_kg"] for key, value in use.items()), "FREIGHT_SHARED_SERVICE_OVERBOOKED")
    check(all(value == 1 for value in movements.values()), "FREIGHT_DUPLICATE_HUB_MOVEMENT")
    consumers = defaultdict(set)
    for trip in state["trips"].values():
        if trip["status"] != "scheduled":
            continue
        parents = trip["baggage_forwarding_ids"]
        ready = custody(trip["baggage_release_ids"], parents, trip["leg"] - 1)
        check(not ready or at(trip["departure_time"]) >= max(ready), "GROUND_BEFORE_FREIGHT_HANDOVER")
        pending, visited = list(parents), set()
        while pending:
            identifier = pending.pop()
            if identifier in visited:
                continue
            visited.add(identifier)
            consumers[identifier].add(trip["group_id"])
            pending.extend(manifests[identifier]["prior_forwarding_ids"])
    handling_by_bag = {o["bag_id"]: o.get("required_handling", "standard") for o in obligations.values()}
    return {"accepted": base["accepted"] and not errors, "errors": sorted(set(errors)), "network_evaluation": base,
        "evaluation_revision": EVALUATION_REVISION,
        "freight_metrics": {"active_manifests": sum(r["status"] == "scheduled" for r in manifests.values()),
            "actual_bag_hub_movements": len(movements), "max_recorded_custody_hops": max(depth.values(), default=0),
            "max_origin_stages_joined": max(origin_joins, default=0),
            "max_ground_group_consumers_per_manifest": max((len(v) for v in consumers.values()), default=0),
            "service_used_kg": dict(use),
            "handling_restricted_movements": sum(1 for identifier, hub in movements
                if handling_by_bag[identifier] != "standard"),
            "operations_events": len(events),
            "cancelled_manifests": sum(r["status"] == "cancelled" for r in manifests.values()),
            "minimum_horizon_proven": False}, "provider_calls": 0}


def evaluate(task, runtime):
    try:
        return _evaluate(task, runtime)
    except (KeyError, ValueError, TypeError, ArithmeticError, RecursionError) as error:
        return {"accepted": False, "errors": ["MALFORMED_FREIGHT:" + type(error).__name__], "provider_calls": 0}
