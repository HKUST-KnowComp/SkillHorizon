"""Construct a consistent active airline slice without rewriting source fixtures.

This is a kernel fixture constructor, not yet a long-horizon task generator.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import random

from integrations.internal_mcp.io import read_json, write_json


TABLES = {"passenger": "passengers", "flight": "flights", "booking": "bookings", "seat": "seats",
          "baggage": "baggage", "payment": "payments", "special_service": "special_services",
          "loyalty_account": "loyalty_accounts", "complaint": "complaints"}


def construct(source, runtime, *, seed=9, passengers=3, capacity=2, background=True):
    source, runtime = Path(source).resolve(), Path(runtime).resolve()
    if runtime.exists() or runtime.is_relative_to(source) or source.is_relative_to(runtime):
        raise ValueError("Use a fresh isolated runtime")
    if type(seed) is not int or type(passengers) is not int or not 1 <= passengers <= 30 or type(capacity) is not int or not 1 <= capacity <= 30:
        raise ValueError("Invalid fixture configuration")
    tables = {name: read_json(source / f"data/database/{filename}.json") for name, filename in TABLES.items()}
    rng = random.Random(seed)
    flight_template = deepcopy(rng.choice(tables["flight"]))
    passenger_templates = rng.sample(tables["passenger"], passengers)
    if not background:
        tables = {name: [] for name in TABLES}
    task_id = f"c09-kernel-{seed}"
    active = {"passengers": [], "flights": []}
    origin = datetime(2026, 10, 8, tzinfo=timezone.utc)
    for i, template in enumerate(passenger_templates):
        identifier = f"{task_id}-p{i + 1}"
        passenger = deepcopy(template)
        passenger.update({"passenger_id": identifier, "id": identifier, "status": "active", "task_id": task_id,
                          "source_passenger_id": template["passenger_id"]})
        tables["passenger"].append(passenger)
        active["passengers"].append(identifier)
    for i in range(3):
        identifier = f"{task_id}-f{i + 1}"
        caps = {"economy": capacity, "business": 1, "first": 0}
        flight = deepcopy(flight_template)
        flight.update({"flight_id": identifier, "id": identifier, "flight_number": f"SH{seed % 1000:03d}{i}",
                       "task_id": task_id, "source_flight_id": flight_template["flight_id"], "status": "scheduled",
                       "total_seats": caps, "available_seats": deepcopy(caps), "base_price": {"economy": 100 + 25 * i, "business": 250 + 25 * i, "first": 0},
                       "scheduled_departure": (origin + timedelta(hours=8 + i * 3)).isoformat().replace("+00:00", "Z"),
                       "scheduled_arrival": (origin + timedelta(hours=10 + i * 3)).isoformat().replace("+00:00", "Z"),
                       "actual_departure": None, "actual_arrival": None, "delay_minutes": 0})
        tables["flight"].append(flight)
        active["flights"].append(identifier)
        for cabin, size in caps.items():
            for j in range(size):
                number = f"{j + 1}{'E' if cabin == 'economy' else 'B'}"
                tables["seat"].append({"seat_id": f"{identifier}-{number}", "flight_id": identifier,
                    "task_id": task_id, "seat_number": number, "seat_class": cabin, "seat_type": "window" if j % 2 == 0 else "aisle",
                    "status": "available", "passenger_id": None, "booking_id": None, "is_exit_row": False,
                    "is_extra_legroom": cabin == "business", "upgrade_price": 0})
    world = {"format": "skillhorizon.c09.kernel.v1", "task_id": task_id, "tables": tables,
             "sequence": 0, "clock_origin": "2026-10-08T00:00:00Z", "active": active,
             "construction": {"source": str(source), "source_ownership": "existing_internal_synthetic_asset",
                 "active_objects": "new_complete_manifests_derived_from_source_templates", "seed": seed,
                 "background_retained": background, "stage": "kernel_fixture_not_long_horizon_task"}}
    write_json(runtime / "world.json", world)
    return world
