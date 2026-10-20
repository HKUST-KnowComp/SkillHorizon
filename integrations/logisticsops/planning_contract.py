"""Explicit input layout for the public, stateless connection-checking script."""

TEXT = {"type": "string", "minLength": 1}
PLANNING_INPUT_SCHEMA = {
    "type": "object", "required": ["flights", "itineraries"],
    "properties": {
        "flights": {"type": "array", "maxItems": 128, "items": {
            "type": "object", "required": ["flight_id", "departure_airport", "arrival_airport",
                "scheduled_departure", "scheduled_arrival", "capacity_for_proposal"],
            "properties": {"flight_id": TEXT, "departure_airport": TEXT, "arrival_airport": TEXT,
                "scheduled_departure": TEXT, "scheduled_arrival": TEXT,
                "capacity_for_proposal": {"type": "integer", "minimum": 0}}}},
        "itineraries": {"type": "array", "maxItems": 64, "items": {
            "type": "object", "required": ["passenger_id", "flight_ids",
                "minimum_connection_minutes", "final_deadline"],
            "properties": {"passenger_id": TEXT,
                "flight_ids": {"type": "array", "minItems": 1, "items": TEXT},
                "minimum_connection_minutes": {"type": "number", "minimum": 0},
                "final_deadline": TEXT}}},
    },
}
