"""Cross-cohort ground plans with multi-origin through-baggage obligations."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta
from pathlib import Path
from skillhorizon.construction_provenance import observe_call
import random

from integrations.internal_mcp.io import read_json, write_json
from .build import build as build_base
from .public_contract import GROUND_TIMING
from integrations.internal_mcp.topology import parent_positions, disclose_pattern


FORMAT = "skillhorizon.c09.transfer_network.v1"


def build(source, output, *, seed=9, cohorts=3, legs=4, background=True, dependency_pattern="staggered"):
    if dependency_pattern not in ("staggered", "distributed"):
        raise ValueError("Airline topology requires its domain-specific transfer construction")
    parent_positions(0, 0, cohorts, seed, dependency_pattern)
    if type(legs) is not int or not 2 <= legs <= 8:
        raise ValueError("Transfer networks require 2..8 legs")
    output = Path(output)
    public = observe_call(build_base, source, output, seed=seed, cohorts=cohorts, legs=legs, background=background, transfer_network=True)
    world = read_json(output / "checkpoint/world.json")
    rng = random.Random(seed + 901)
    people = public["requests"]
    bags = {b["booking_id"]: b for b in world["tables"]["baggage"] if b.get("task_id") == public["task_id"]}
    obligations, groups, vehicles = [], [], []
    for person_index, request in enumerate(people):
        for origin in range(legs - 1):
            bag = bags[request["bookings"][origin]]
            bag["weight_kg"] = rng.choice((8, 12, 16, 20))
            age = 1 + (person_index + 2 * origin) % min(3, legs - origin - 1)
            if dependency_pattern == "distributed":
                age = random.Random(f"baggage-distributed-v1:{seed}:{person_index}:{origin}").randrange(1, legs - origin)
            delivery = origin + age
            obligations.append({"obligation_id": f"cargo-{person_index}-{origin}", "passenger_id": request["passenger_id"],
                "bag_id": bag["baggage_id"], "origin_booking_id": request["bookings"][origin], "origin_leg": origin + 1,
                "delivery_leg": delivery + 1, "handling_minutes": 10})
    for leg in range(1, legs):
        rotation = 1 + (leg - 1) % (cohorts - 1)
        if dependency_pattern == "distributed":
            rotation = random.Random(f"freight-distributed-v1:{seed}:{leg}").randrange(1, cohorts)
        for cohort in range(cohorts):
            left = people[2 * cohort]
            right = people[2 * ((cohort + rotation) % cohorts) + 1]
            groups.append({"group_id": f"ground-{leg + 1}-{cohort}", "leg": leg + 1,
                "passenger_ids": [left["passenger_id"], right["passenger_id"]], "boarding_buffer_minutes": 20})
        for number, capacity, duration in ((0, 36, 15), (1, 160, 25), (2, 160, 30)):
            vehicles.append({"vehicle_id": f"vehicle-{leg + 1}-{number}", "leg": leg + 1,
                "passenger_capacity": 2, "weight_capacity_kg": capacity, "wheelchair_capacity": 2,
                "base_travel_minutes": duration, "weight_step_kg": 20, "minutes_per_weight_step": 5,
                "wheelchair_minutes": 5, "turnaround_minutes": 10})
    # Cross-cohort joins need a feasible shared early inbound option. Scarcity remains at the final leg.
    for leg in range(legs - 1):
        identifier = public["route"][leg]["alternative_flight_ids"][0]
        flight = next(f for f in world["tables"]["flight"] if f["flight_id"] == identifier)
        existing = len([s for s in world["tables"]["seat"] if s["flight_id"] == identifier])
        flight["total_seats"]["economy"] = flight["available_seats"]["economy"] = len(people)
        template = next(s for s in world["tables"]["seat"] if s["flight_id"] == identifier)
        for index in range(existing, len(people)):
            seat = deepcopy(template)
            seat.update(seat_id=f"{identifier}-{index + 1}E", seat_number=f"{index + 1}E")
            world["tables"]["seat"].append(seat)
    for route in public["route"]:
        for flight in world["tables"]["flight"]:
            if flight["flight_id"] in [route["disrupted_flight_id"], *route["alternative_flight_ids"]]:
                for field in ("scheduled_departure", "scheduled_arrival"):
                    value = datetime.fromisoformat(flight[field].replace("Z", "+00:00")) + timedelta(hours=2 * (route["leg"] - 1))
                    flight[field] = value.isoformat().replace("+00:00", "Z")
    for request in people:
        value = datetime.fromisoformat(request["final_deadline"].replace("Z", "+00:00")) + timedelta(hours=2 * (legs - 1))
        request["final_deadline"] = value.isoformat().replace("+00:00", "Z")
    public["format"] = world["format"] = FORMAT
    disclose_pattern(public, dependency_pattern, seed)
    public["max_net_spend_usd"] = legs * len(people) * 130 + 150
    public["goal"] += " Also schedule every cross-cohort airport transfer. Consume all due through-baggage releases from their actual origin bookings, preserve future obligations, and meet onward boarding times."
    public["transfer_network"] = {"groups": groups, "vehicles": vehicles, "baggage_obligations": obligations,
        "policy": "One group per scheduled vehicle trip. Same-vehicle trips require explicit prior-trip linkage and turnaround. All times are synthetic operational plans. " + GROUND_TIMING}
    skills = {
        "release-through-baggage": "# Release through-baggage obligations\n\nInspect the public origin/delivery obligations, actual bag records and origin clearance. Release a bag only against its own current cleared booking. Preserve its measured weight and origin identity; keep future-due releases available for later airport transfers. A current passenger clearance is not a substitute for another bag's origin evidence.\n",
        "schedule-cross-cohort-transfer": "# Schedule a shared airport transfer\n\nInspect group membership, both inbound clearances, all baggage due from different origins and actual vehicle trips. Compute total weight and wheelchair demand. Choose a vehicle that fits, derive departure after the latest inbound/baggage readiness and prior-trip turnaround, and calculate load-dependent arrival. Bind the actual trip when issuing each onward clearance. Reuse unaffected history; replace stale trips and dependent clearances after itinerary repair. No reference vehicle or gold next skill is supplied.\n"}
    for name, content in skills.items():
        if name == "schedule-cross-cohort-transfer":
            content += "\n## Exact timing rules\n\n" + GROUND_TIMING + "\n"
        path = output / "public/skills" / name / "SKILL.md"
        path.parent.mkdir(parents=True)
        path.write_text(content, encoding="utf-8")
        public["skills"].append({"name": name, "files": ["SKILL.md"]})
    world["transfer_network"] = {"releases": {}, "trips": {}, "next_id": 1}
    private = read_json(output / "private/initial_obligations.json")
    private["public"] = deepcopy(public)
    for relative in ("checkpoint/world.json", "runtime/world.json"):
        write_json(output / relative, world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    config = read_json(output / "mcp.json")
    config["mcpServers"]["c09-airline"]["args"][1] = "integrations.logisticsops.network_server"
    write_json(output / "mcp.json", config)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=9)
    parser.add_argument("--cohorts", type=int, default=3)
    parser.add_argument("--legs", type=int, default=4)
    print(build(**vars(parser.parse_args()))["task_id"])
