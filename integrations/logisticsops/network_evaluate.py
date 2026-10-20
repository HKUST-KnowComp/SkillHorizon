"""Independent physical/time joins over the independent base travel checker.

No runtime or witness helpers are imported. Current chosen plans are checked against frozen source
obligations; obsolete/cancelled plans remain history, not active transport or duplicated delivery.
"""

from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from integrations.internal_mcp.io import read_json
from .evaluate import evaluate_world


def at(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Timezone required")
    return result


def _evaluate(task, runtime, *, expected_format="skillhorizon.c09.transfer_network.v1", active_freight_release_ids=()):
    task, runtime = Path(task), Path(runtime)
    initial = read_json(task / "checkpoint/world.json")
    obligations = read_json(task / "private/initial_obligations.json")
    world = read_json(runtime / "world.json")
    base = evaluate_world(obligations, initial, world)
    public = obligations["public"]
    spec = public["transfer_network"]
    state = world["transfer_network"]
    clearances = world["connection_state"]["clearances"]
    release_by_id, trips = state["releases"], state["trips"]
    people = {r["passenger_id"]: r for r in public["requests"]}
    bags = {r["baggage_id"]: r for r in world["tables"]["baggage"]}
    old_bags = {r["baggage_id"]: r for r in initial["tables"]["baggage"]}
    flights = {r["flight_id"]: r for r in initial["tables"]["flight"]}
    groups = {g["group_id"]: g for g in spec["groups"]}
    vehicles = {v["vehicle_id"]: v for v in spec["vehicles"]}
    due = {r["obligation_id"]: r for r in spec["baggage_obligations"]}
    base_valid = {i for ids in base["valid_clearances"].values() for i in ids}
    errors, valid, active, graph = [], {}, set(), {}
    loads, ages, origin_joins, queues = [], Counter(), [], []

    def check(condition, code):
        if not condition:
            errors.append(code)
        return condition

    check(read_json(task / "public/task.json") == public, "PUBLIC_NETWORK_OBLIGATIONS_CHANGED")
    check(initial["format"] == world["format"] == public["format"] == expected_format, "NETWORK_FORMAT")

    def verify(kind, identifier):
        key = (kind, identifier)
        if key in valid:
            return valid[key]
        if key in active:
            check(False, "NETWORK_DEPENDENCY_CYCLE")
            return False
        active.add(key)
        parents, ok = [], True
        if kind == "clearance":
            row = clearances[identifier]
            ok = identifier in base_valid
            if row["previous_clearance_id"]:
                parents.append(("clearance", row["previous_clearance_id"]))
            if row["leg"] > 1:
                trip = trips.get(row.get("transfer_trip_id"))
                ok = bool(trip) and ok
                if trip:
                    group = groups[trip["group_id"]]
                    ok = check(row["leg"] == group["leg"] and row["passenger_id"] in group["passenger_ids"]
                        and row["previous_clearance_id"] in trip["inbound_clearance_ids"], "ONWARD_EXACT_GROUP_JOIN") and ok
                    flight = flights[row["snapshot"]["booking"]["flight_id"]]
                    ok = check(at(trip["arrival_time"]) + timedelta(minutes=group["boarding_buffer_minutes"]) <= at(flight["scheduled_departure"]), "ONWARD_BOARDING_AFTER_GROUND_ARRIVAL") and ok
                    parents.append(("trip", trip["trip_id"]))
            else:
                ok = check(not row.get("transfer_trip_id"), "INITIAL_CLEARANCE_HAS_TRANSFER") and ok
        elif kind == "release":
            row = release_by_id[identifier]
            obligation = due[row["obligation_id"]]
            origin = clearances[row["origin_clearance_id"]]
            bag = bags[obligation["bag_id"]]
            frozen_bag = old_bags[obligation["bag_id"]]
            ok = check(all(row.get(k) == v for k, v in obligation.items()) and origin["booking_id"] == obligation["origin_booking_id"] and origin["leg"] == obligation["origin_leg"], "FROZEN_CARGO_ORIGIN_AND_MATURITY") and ok
            fixed_fields = set(frozen_bag) - {"flight_id", "booking_revision"}
            ok = check(all(bag[k] == frozen_bag[k] for k in fixed_fields) and row["bag"] == bag
                and bag["booking_revision"] == origin["snapshot"]["booking"]["revision"]
                and bag["flight_id"] == origin["snapshot"]["booking"]["flight_id"], "ACTUAL_ORIGIN_BAG_AND_WEIGHT") and ok
            parents.append(("clearance", row["origin_clearance_id"]))
        else:
            row = trips[identifier]
            group, vehicle = groups[row["group_id"]], vehicles[row["vehicle_id"]]
            inbound = [clearances[i] for i in row["inbound_clearance_ids"]]
            cargo = [release_by_id[i] for i in row["baggage_release_ids"]]
            expected = {o["obligation_id"] for o in due.values() if o["delivery_leg"] == group["leg"] and o["passenger_id"] in group["passenger_ids"]}
            ok = check(row["status"] == "scheduled" and row["leg"] == group["leg"] == vehicle["leg"], "ACTIVE_TRANSFER_STAGE") and ok
            ok = check(len(inbound) == len(group["passenger_ids"]) and {r["passenger_id"] for r in inbound} == set(group["passenger_ids"])
                and all(c["leg"] == group["leg"] - 1 for c in inbound), "CROSS_COHORT_INBOUND_IDENTITIES") and ok
            ok = check(len(cargo) == len(expected) and {r["obligation_id"] for r in cargo} == expected, "EXACT_DUE_CARGO_IDENTITIES") and ok
            weight = sum(old_bags[due[r["obligation_id"]]["bag_id"]]["weight_kg"] for r in cargo)
            chairs = sum("wheelchair" in people[p]["required_ground_services"] for p in group["passenger_ids"])
            ok = check(row["weight_kg"] == weight and row["wheelchair_count"] == chairs and weight <= vehicle["weight_capacity_kg"]
                and len(inbound) <= vehicle["passenger_capacity"] and chairs <= vehicle["wheelchair_capacity"], "VEHICLE_PHYSICAL_LOAD") and ok
            ready = [at(flights[c["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) for c in inbound]
            ready += [at(flights[clearances[r["origin_clearance_id"]]["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) + timedelta(minutes=due[r["obligation_id"]]["handling_minutes"]) for r in cargo]
            earliest = max(ready)
            prior = None
            for other in trips.values():
                if other["trip_id"] == identifier:
                    break
                if other["vehicle_id"] == vehicle["vehicle_id"] and other["status"] == "scheduled":
                    prior = other
            ok = check(row.get("previous_vehicle_trip_id") == (prior["trip_id"] if prior else None), "VEHICLE_PREVIOUS_PLAN_IDENTITY") and ok
            if prior:
                ready.append(at(prior["arrival_time"]) + timedelta(minutes=vehicle["turnaround_minutes"]))
                parents.append(("trip", prior["trip_id"]))
            duration = vehicle["base_travel_minutes"] + ((weight + vehicle["weight_step_kg"] - 1) // vehicle["weight_step_kg"]) * vehicle["minutes_per_weight_step"] + chairs * vehicle["wheelchair_minutes"]
            ok = check(row["duration_minutes"] == duration and at(row["departure_time"]) >= max(ready)
                and at(row["arrival_time"]) == at(row["departure_time"]) + timedelta(minutes=duration), "GROUND_DERIVED_TIME_AND_TURNAROUND") and ok
            queues.append(max(0, int((max(ready) - earliest).total_seconds() / 60)))
            loads.append(weight)
            ages.update(group["leg"] - r["origin_leg"] for r in cargo if r["bag"]["weight_kg"] > 0)
            origin_joins.append(len({r["origin_leg"] for r in cargo if r["bag"]["weight_kg"] > 0}))
            parents += [("clearance", i) for i in row["inbound_clearance_ids"]] + [("release", i) for i in row["baggage_release_ids"]]
        parent_results = [verify(*parent) for parent in parents]
        ok = all(parent_results) and ok
        graph[key] = parents
        active.remove(key)
        valid[key] = ok
        return ok

    completed, chosen_roots = 0, []
    for request in public["requests"]:
        for booking_id in request["bookings"]:
            candidates = base["valid_clearances"].get(booking_id, [])
            chosen, candidate_errors = None, []
            for identifier in reversed(candidates):
                prior_errors = len(errors)
                if verify("clearance", identifier):
                    chosen = ("clearance", identifier)
                    break
                candidate_errors.extend(errors[prior_errors:])
                del errors[prior_errors:]
            if chosen:
                completed += 1
                chosen_roots.append(chosen)
            else:
                errors.extend(candidate_errors)
                check(False, "NETWORK_BOOKING_NOT_COMPLETE:" + booking_id)
    # Active freight can be orphaned from the chosen passenger itinerary. Its
    # source still has to be current; preserving history does not validate it.
    for identifier in set(active_freight_release_ids):
        check(verify("release", identifier), "ACTIVE_FREIGHT_SOURCE_NOT_CURRENT:" + identifier)
    scheduled = [r for r in trips.values() if r["status"] == "scheduled"]
    check(len({t["group_id"] for t in scheduled}) == len(scheduled), "DUPLICATE_ACTIVE_TRANSFER_GROUP")
    applied = [release_by_id[i]["obligation_id"] for t in scheduled for i in t["baggage_release_ids"]]
    check(len(applied) == len(set(applied)), "CARGO_DELIVERY_DOUBLE_CONSUMPTION")
    if completed == sum(len(r["bookings"]) for r in public["requests"]):
        check({r["group_id"] for r in scheduled} == set(groups) and set(applied) == set(due), "FINAL_NETWORK_OBLIGATION_COVERAGE")
    closure = set()

    def include(node):
        if node not in closure:
            closure.add(node)
            for parent in graph[node]:
                include(parent)

    for node in chosen_roots:
        include(node)
    graph = {node: graph[node] for node in closure}
    ages, origin_joins, queues = Counter(), [], []
    for kind, identifier in graph:
        if kind != "trip":
            continue
        row = trips[identifier]
        group, vehicle = groups[row["group_id"]], vehicles[row["vehicle_id"]]
        cargo = [release_by_id[i] for i in row["baggage_release_ids"]]
        ages.update(group["leg"] - r["origin_leg"] for r in cargo if r["bag"]["weight_kg"] > 0)
        origin_joins.append(len({r["origin_leg"] for r in cargo if r["bag"]["weight_kg"] > 0}))
        ready = [at(flights[clearances[i]["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) for i in row["inbound_clearance_ids"]]
        ready += [at(flights[clearances[r["origin_clearance_id"]]["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) + timedelta(minutes=due[r["obligation_id"]]["handling_minutes"]) for r in cargo]
        if row.get("previous_vehicle_trip_id"):
            previous = trips[row["previous_vehicle_trip_id"]]
            queues.append(max(0, int((at(previous["arrival_time"]) + timedelta(minutes=vehicle["turnaround_minutes"]) - max(ready)).total_seconds() / 60)))
    depth_cache = {}

    def depth(node):
        if node not in depth_cache:
            depth_cache[node] = 1 + max((depth(p) for p in graph[node]), default=0)
        return depth_cache[node]

    consumers = Counter(p for parents in graph.values() for p in parents)
    metrics = {"recorded_business_dependency_depth": max((depth(n) for n in graph), default=0) if "NETWORK_DEPENDENCY_CYCLE" not in errors else None,
        "recorded_business_nodes": len(graph), "recorded_business_edges": sum(len(p) for p in graph.values()),
        "multi_parent_nodes": sum(len(set(p)) > 1 for p in graph.values()), "max_fanout": max(consumers.values(), default=0),
        "positive_cargo_age_histogram": dict(sorted(ages.items())), "max_positive_origins_joined_per_trip": max(origin_joins, default=0),
        "resource_waiting_trips": sum(n > 0 for n in queues), "max_resource_wait_minutes": max(queues, default=0),
        "applied_cargo_obligations": len(applied), "outstanding_obligations": len(set(due) - set(applied)),
        "minimum_plan_proven": False, "scope": "Current clearance/release/ground-trip graph; excludes freight manifests and custody edges, call depth and model behavior"}
    return {"accepted": base["success"] and not errors, "errors": sorted(set(errors)), "base_evaluation": base,
        "completed_bookings": completed, "required_bookings": sum(len(r["bookings"]) for r in public["requests"]), "metrics": metrics, "provider_calls": 0}


def evaluate(task, runtime):
    try:
        return _evaluate(task, runtime)
    except (KeyError, ValueError, TypeError, ArithmeticError, RecursionError) as error:
        return {"accepted": False, "errors": ["MALFORMED_NETWORK:" + type(error).__name__], "provider_calls": 0}
