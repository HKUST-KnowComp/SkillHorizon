"""Source-derived multi-passenger, multi-leg connection recovery tasks."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json
from .seed import TABLES
from .public_contract import VERSION, BOOKING_IDENTITY, PAYMENT_RECOVERY


FORMAT = "skillhorizon.c09.connections.v1"
SKILLS = Path(__file__).with_name("skills")


def iso(value):
    return value.isoformat().replace("+00:00", "Z")


def route_chain(flights, length, rng):
    by_origin = {}
    for flight in flights:
        by_origin.setdefault(flight["departure_airport"], []).append(flight)
    starts = sorted(by_origin)
    rng.shuffle(starts)

    def walk(airport, chain):
        if len(chain) == length:
            return chain
        options = list(by_origin.get(airport, []))
        rng.shuffle(options)
        seen_destinations = set()
        for option in options:
            destination = option["arrival_airport"]
            if destination in seen_destinations:
                continue
            seen_destinations.add(destination)
            result = walk(destination, chain + [option])
            if result:
                return result
        return None

    for start in starts:
        result = walk(start, [])
        if result:
            return result
    raise ValueError("Source graph has no connected route chain of requested length")


def build(source, output, *, seed=9, cohorts=2, legs=3, background=True, transfer_network=False):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a fresh isolated task directory")
    if type(seed) is not int or type(cohorts) is not int or not 2 <= cohorts <= 64 or type(legs) is not int or not 2 <= legs <= 8:
        raise ValueError("Use an integer seed, 2..64 two-person cohorts and 2..8 connected legs")
    source_tables = {name: read_json(source / f"data/database/{filename}.json") for name, filename in TABLES.items()}
    rng = random.Random(seed)
    templates = route_chain(source_tables["flight"], legs, rng)
    people = rng.sample(source_tables["passenger"], cohorts * 2)
    tables = deepcopy(source_tables) if background else {name: [] for name in TABLES}
    task_id = f"c09-{'transfer' if transfer_network else 'connection'}-{seed}-{cohorts}c-{legs}l"
    origin = datetime(2026, 10, 8, 8, tzinfo=timezone.utc)
    requests, catalog, initial_ids = [], [], {"booking": [], "baggage": [], "special_service": [], "payment": []}
    for i, person in enumerate(people):
        identifier = f"{task_id}-p{i + 1}"
        passenger = {**deepcopy(person), "passenger_id": identifier, "id": identifier, "task_id": task_id,
                     "status": "active", "source_passenger_id": person["passenger_id"]}
        tables["passenger"].append(passenger)
        requests.append({"passenger_id": identifier, "cohort_id": f"cohort-{i // 2 + 1}", "bookings": [],
            "final_deadline": iso(origin + timedelta(hours=(legs - 1) * 4 + (2 if i >= len(people) - 2 else 6))),
            "minimum_connection_minutes": 60, "required_ground_services": ["checked_baggage", "wheelchair"] if i % 2 == 0 else ["checked_baggage"]})
    for leg, template in enumerate(templates):
        group = []
        for option in range(4):
            identifier = f"{task_id}-leg{leg + 1}-f{option}"
            capacity = len(people) if option in {0, 3} else (2 if option == 1 else len(people) - 2)
            depart = origin + timedelta(hours=leg * 4 + (option - 1) * 2 if option else leg * 4 - 1)
            fare = 100 if option == 0 else (140 - option * 10)
            caps = {"economy": capacity, "business": 0, "first": 0}
            flight = {**deepcopy(template), "flight_id": identifier, "id": identifier, "task_id": task_id,
                "source_flight_id": template["flight_id"], "flight_number": f"SH{leg + 1:02d}{option}",
                "status": "cancelled" if option == 0 else "scheduled", "total_seats": caps,
                "available_seats": {"economy": 0 if option == 0 else capacity, "business": 0, "first": 0},
                "base_price": {"economy": fare, "business": 0, "first": 0}, "scheduled_departure": iso(depart),
                "scheduled_arrival": iso(depart + timedelta(hours=2)), "actual_departure": None, "actual_arrival": None,
                "delay_minutes": 0}
            tables["flight"].append(flight)
            for seat in range(capacity):
                number = f"{seat + 1}E"
                tables["seat"].append({"seat_id": f"{identifier}-{number}", "task_id": task_id, "flight_id": identifier,
                    "seat_number": number, "seat_class": "economy", "seat_type": "window" if seat % 2 == 0 else "aisle",
                    "status": "available", "passenger_id": None, "booking_id": None, "is_exit_row": False,
                    "is_extra_legroom": False, "upgrade_price": 0})
            group.append(identifier)
        catalog.append({"leg": leg + 1, "departure_airport": template["departure_airport"], "arrival_airport": template["arrival_airport"],
                        "disrupted_flight_id": group[0], "alternative_flight_ids": group[1:]})
        for i, request in enumerate(requests):
            booking_id = f"{task_id}-p{i + 1}-leg{leg + 1}-booking"
            passenger_id = request["passenger_id"]
            request["bookings"].append(booking_id)
            row = {"booking_id": booking_id, "task_id": task_id, "passenger_id": passenger_id, "flight_id": group[0],
                "booking_class": "economy", "booking_channel": "agent", "status": "confirmed", "seat_number": None,
                "checked_in": False, "ticket_price": 100, "change_fee": 0, "paid_amount": 100, "revision": 1,
                "itinerary_history": [], "created_at": "2026-10-01T00:00:00Z", "updated_at": "2026-10-01T00:00:00Z"}
            tables["booking"].append(row)
            initial_ids["booking"].append(booking_id)
            payment_id = f"{booking_id}-charge"
            tables["payment"].append({"payment_id": payment_id, "task_id": task_id, "booking_id": booking_id,
                "amount": 100, "currency": "USD", "method": "credit_card", "direction": "charge", "status": "completed",
                "booking_revision": 1, "reference_number": payment_id, "created_at": row["created_at"], "processed_at": row["created_at"]})
            initial_ids["payment"].append(payment_id)
            bag_id = f"{booking_id}-bag"
            tables["baggage"].append({"baggage_id": bag_id, "task_id": task_id, "booking_id": booking_id,
                "passenger_id": passenger_id, "flight_id": group[0], "booking_revision": 1, "weight_kg": 20,
                "baggage_type": "checked", "status": "checked_in", "tag_number": bag_id, "claim_amount": 0,
                "claim_status": "none", "created_at": row["created_at"]})
            initial_ids["baggage"].append(bag_id)
            if i % 2 == 0:
                service_id = f"{booking_id}-wheelchair"
                tables["special_service"].append({"service_id": service_id, "task_id": task_id, "booking_id": booking_id,
                    "passenger_id": passenger_id, "flight_id": group[0], "booking_revision": 1,
                    "service_type": "wheelchair", "status": "requested", "notes": "Accessibility service required for each connection.",
                    "created_at": row["created_at"]})
                initial_ids["special_service"].append(service_id)
    reference_spend = legs * (2 * 130 + (len(people) - 2) * 110)
    public = {"format": FORMAT, "task_id": task_id, "public_contract_version": VERSION,
        "goal": "Recover the original bookings affected by cancelled flights for the travel cohorts. Preserve passenger and ground-service identities, "
            "meet final arrival deadlines and minimum connection times, settle fares using the existing paid balances, "
            "and obtain current connection clearances for every leg. Shared flight capacities and the total net-spend "
            "limit apply across all cohorts. Alternative itineraries, interleaving and recovery are legal.",
        "requests": requests, "route": catalog, "max_net_spend_usd": reference_spend + 150,
        "policy": {"currency": "USD", "voluntary_change_fee_usd": 75, "cancelled_flight_change_fee_usd": 0,
            "booking_identity_and_cancellation": BOOKING_IDENTITY, "payment_recovery": PAYMENT_RECOVERY,
            "scope": "synthetic_predeparture_service_desk_not_live_airline", "public_state_rereads": True,
            "skills_are_observational": True, "reference_solution_exposed": False},
        "skills": [{"name": p.name, "files": ["SKILL.md"] + (["scripts/check_connections.py"] if (p / "scripts").is_dir() else [])}
                   for p in sorted(SKILLS.iterdir()) if p.is_dir()]}
    world = {"format": FORMAT, "task_id": task_id, "clock_origin": "2026-10-08T00:00:00Z", "sequence": 0,
        "tables": tables, "active": {"passengers": [r["passenger_id"] for r in requests],
                                      "flights": [r["flight_id"] for r in tables["flight"] if r.get("task_id") == task_id]},
        "connection_state": {"clearances": {}, "skill_runs": {}, "active_skill_run": None, "next_clearance": 1, "next_skill_run": 1},
        "construction": {"source": str(source), "ownership": "internal_source_plus_new_synthetic_connected_itineraries",
                         "seed": seed, "cohorts": cohorts, "legs": legs, "background_retained": background}}
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_ids": initial_ids})
    shutil.copytree(output / "checkpoint", output / "runtime")
    materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    write_json(output / "mcp.json", {"mcpServers": {"c09-airline": {"command": sys.executable,
        "args": ["-m", "integrations.logisticsops.server", str(output), str(output / "runtime")],
        "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=9)
    parser.add_argument("--cohorts", type=int, default=2)
    parser.add_argument("--legs", type=int, default=3)
    args = parser.parse_args()
    public = build(args.source, args.output, seed=args.seed, cohorts=args.cohorts, legs=args.legs)
    print({"task_id": public["task_id"], "provider_calls": 0, "qualified": False})


if __name__ == "__main__":
    main()
