"""Check an actor-supplied itinerary proposal using only explicit JSON input."""

from collections import Counter
from datetime import datetime
import json
import sys


class InputError(ValueError):
    def __init__(self, path, message):
        super().__init__(message)
        self.details = {"path": path, "message": message}


def time(value, path):
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        raise InputError(path, "Use an ISO timestamp with an explicit timezone offset.") from None
    if parsed.tzinfo is None:
        raise InputError(path, "Times must include timezone offsets.")
    return parsed


def check(data):
    flights = {f["flight_id"]: f for f in data["flights"]}
    flight_paths = {f["flight_id"]: f"$/arguments/inputs/flights/{i}"
                    for i, f in enumerate(data["flights"])}
    if len(flights) != len(data["flights"]):
        raise InputError("$/arguments/inputs/flights", "Each flight_id must be unique.")
    if len(flights) > 128 or len(data["itineraries"]) > 64:
        raise InputError("$/arguments/inputs", "At most 128 flights and 64 itineraries are accepted.")
    times = {identifier: {field: time(flight[field], flight_paths[identifier] + "/" + field)
                         for field in ("scheduled_departure", "scheduled_arrival")}
             for identifier, flight in flights.items()}
    uses, issues = Counter(), []
    for index, item in enumerate(data["itineraries"]):
        path = f"$/arguments/inputs/itineraries/{index}"
        for offset, identifier in enumerate(item["flight_ids"]):
            if identifier not in flights:
                raise InputError(f"{path}/flight_ids/{offset}", "Flight ID is missing from the supplied flights array.")
        selected = [flights[identifier] for identifier in item["flight_ids"]]
        if not selected:
            raise InputError(path + "/flight_ids", "An itinerary cannot be empty.")
        for flight in selected:
            uses[flight["flight_id"]] += 1
        for previous, following in zip(selected, selected[1:]):
            gap = (times[following["flight_id"]]["scheduled_departure"]
                   - times[previous["flight_id"]]["scheduled_arrival"]).total_seconds() / 60
            if previous["arrival_airport"] != following["departure_airport"] or gap < item["minimum_connection_minutes"]:
                issues.append({"passenger_id": item["passenger_id"], "kind": "connection", "from": previous["flight_id"], "to": following["flight_id"]})
        if times[selected[-1]["flight_id"]]["scheduled_arrival"] > time(item["final_deadline"], path + "/final_deadline"):
            issues.append({"passenger_id": item["passenger_id"], "kind": "deadline"})
    for identifier, count in uses.items():
        if count > flights[identifier]["capacity_for_proposal"]:
            issues.append({"kind": "capacity", "flight_id": identifier, "proposed": count, "capacity": flights[identifier]["capacity_for_proposal"]})
    return {"proposal_feasible": not issues, "issues": issues, "reservations_made": 0,
            "checked_itineraries": len(data["itineraries"]), "supplied_flights": len(flights),
            "scope": "checks_only_supplied_public_data_not_live_inventory"}


if __name__ == "__main__":
    try:
        output = check(json.load(sys.stdin))
    except InputError as error:
        output = {"input_error": error.details}
    except (KeyError, TypeError, ValueError):
        output = {"input_error": {"path": "$/arguments/inputs",
            "message": "Input must match the public flights and itineraries schema."}}
    print(json.dumps(output, allow_nan=False))
