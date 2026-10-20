"""Independent raw-table travel evaluation; never imported by the MCP server."""

from collections import Counter
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from integrations.internal_mcp.io import read_json


def amount(value):
    number = Decimal(str(value))
    if not number.is_finite() or number * 100 != (number * 100).to_integral_value():
        raise ValueError("invalid USD amount")
    return int(number * 100)


def timestamp(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone missing")
    return result


def evaluate_world(obligations, initial, world):
    errors = []

    def check(condition, message):
        if not condition:
            errors.append(message)

    public = obligations["public"]
    task_id = public["task_id"]
    check(world["task_id"] == task_id, "task_identity")
    before, tables = initial["tables"], world["tables"]
    for name, old_rows in before.items():
        check([r for r in old_rows if r.get("task_id") != task_id] ==
              [r for r in tables[name] if r.get("task_id") != task_id], f"background_changed:{name}")
    keys = {"booking": "booking_id", "flight": "flight_id", "seat": "seat_id", "payment": "payment_id",
            "baggage": "baggage_id", "special_service": "service_id", "passenger": "passenger_id"}
    active = {name: {r[pk]: r for r in tables[name] if r.get("task_id") == task_id} for name, pk in keys.items()}
    original = {name: {r[pk]: r for r in before[name] if r.get("task_id") == task_id} for name, pk in keys.items()}
    for name, rows in active.items():
        check(len(rows) == sum(r.get("task_id") == task_id for r in tables[name]), f"duplicate_ids:{name}")
    check(active["passenger"] == original["passenger"], "passengers_changed")
    for identifier, old in original["flight"].items():
        current = active["flight"].get(identifier, {})
        check({k: v for k, v in current.items() if k != "available_seats"} ==
              {k: v for k, v in old.items() if k != "available_seats"}, f"flight_definition_changed:{identifier}")
    for identifier, old in original["payment"].items():
        check(active["payment"].get(identifier) == old, f"original_payment_changed:{identifier}")
    for kind, mutable in (("baggage", {"flight_id", "booking_revision", "transfer_history", "updated_at", "status", "cancelled_at"}),
                          ("special_service", {"flight_id", "booking_revision", "transfer_history", "updated_at", "status", "cancelled_at"})):
        for identifier, old in original[kind].items():
            current = active[kind].get(identifier, {})
            check({k: v for k, v in current.items() if k not in mutable} == {k: v for k, v in old.items() if k not in mutable}, f"original_service_identity:{identifier}")
    payments = list(active["payment"].values())
    net_spend = 0
    for payment in payments:
        check(payment["booking_id"] in active["booking"], f"orphan_payment:{payment['payment_id']}")
        check(payment["direction"] in {"charge", "refund"} and payment["currency"] == "USD"
              and amount(payment["amount"]) > 0 and payment["method"] in {"credit_card", "debit_card", "bank_transfer"}, f"invalid_payment:{payment['payment_id']}")
        if payment["status"] == "completed":
            net_spend += (1 if payment["direction"] == "charge" else -1) * amount(payment["amount"])
    check(net_spend <= amount(public["max_net_spend_usd"]), "net_spend_limit")
    bookings = active["booking"]
    for booking in bookings.values():
        ledger = [p for p in payments if p["booking_id"] == booking["booking_id"] and p["status"] == "completed"]
        charged = sum(amount(p["amount"]) for p in ledger if p["direction"] == "charge")
        refunded = sum(amount(p["amount"]) for p in ledger if p["direction"] == "refund")
        check(charged == amount(booking["paid_amount"]) and 0 <= refunded <= charged, f"ledger_conservation:{booking['booking_id']}")
        for method in {p["method"] for p in ledger}:
            check(sum((1 if p["direction"] == "charge" else -1) * amount(p["amount"]) for p in ledger if p["method"] == method) >= 0,
                  f"refund_method_conservation:{booking['booking_id']}")
        if refunded:
            check(booking["status"] == "cancelled" and refunded <= amount(booking.get("refund_entitlement", 0)), f"refund_entitlement:{booking['booking_id']}")
    live = [b for b in bookings.values() if b["status"] in {"confirmed", "checked_in"}]
    live_pairs = [(b["passenger_id"], b["flight_id"]) for b in live]
    check(len(live_pairs) == len(set(live_pairs)), "duplicate_live_booking")
    for flight in active["flight"].values():
        seats = [s for s in active["seat"].values() if s["flight_id"] == flight["flight_id"]]
        for cabin, capacity in flight["total_seats"].items():
            reserved = sum(b["flight_id"] == flight["flight_id"] and b["booking_class"] == cabin for b in live)
            check(0 <= reserved <= capacity and flight["available_seats"][cabin] == capacity - reserved, f"capacity:{flight['flight_id']}:{cabin}")
            check(sum(s["seat_class"] == cabin for s in seats) == capacity, f"seat_manifest:{flight['flight_id']}:{cabin}")
        check(len({s["seat_number"] for s in seats}) == len(seats), f"duplicate_seat_number:{flight['flight_id']}")
        for seat in seats:
            owners = [b for b in live if b["flight_id"] == flight["flight_id"] and b.get("seat_number") == seat["seat_number"]]
            check(len(owners) <= 1, f"seat_double_owner:{seat['seat_id']}")
            if owners:
                b = owners[0]
                check(seat["status"] == "occupied" and seat.get("booking_id") == b["booking_id"]
                      and seat.get("passenger_id") == b["passenger_id"] and seat["seat_class"] == b["booking_class"], f"seat_identity:{seat['seat_id']}")
            else:
                check(seat["status"] == "available" and not seat.get("passenger_id") and not seat.get("booking_id"), f"orphan_seat:{seat['seat_id']}")

    def expected_snapshot(booking):
        result = {"booking": {key: booking.get(key) for key in ("booking_id", "passenger_id", "flight_id", "booking_class", "revision", "seat_number", "status", "paid_amount", "ticket_price", "change_fee")},
            "payment_ids": sorted(p["payment_id"] for p in payments if p["booking_id"] == booking["booking_id"] and p["status"] == "completed")}
        for kind, pk in (("baggage", "baggage_id"), ("special_service", "service_id")):
            result[kind] = sorted([{key: r.get(key) for key in (pk, "passenger_id", "flight_id", "booking_revision", "status")}
                for r in active[kind].values() if r["booking_id"] == booking["booking_id"]], key=lambda r: r[pk])
        return result

    valid_clearances, completed_bookings = {}, 0
    expected_booking_ids = {b for r in public["requests"] for b in r["bookings"]}
    check(all(b["booking_id"] in expected_booking_ids for b in live), "unexpected_live_booking")
    for request in public["requests"]:
        previous_flight, previous_valid = None, {""}
        for index, identifier in enumerate(request["bookings"]):
            booking = bookings.get(identifier)
            if booking is None:
                errors.append(f"missing_booking:{identifier}")
                previous_valid = set()
                continue
            error_count = len(errors)
            check(booking["passenger_id"] == request["passenger_id"], f"booking_passenger:{identifier}")
            check(booking["status"] == "checked_in" and booking["checked_in"] is True, f"not_checked_in:{identifier}")
            flight = active["flight"].get(booking["flight_id"])
            if flight is None:
                errors.append(f"missing_flight:{identifier}")
                previous_valid = set()
                continue
            route = public["route"][index]
            check(flight["status"] in {"scheduled", "boarding"} and flight["departure_airport"] == route["departure_airport"]
                  and flight["arrival_airport"] == route["arrival_airport"], f"wrong_route_or_status:{identifier}")
            if previous_flight:
                check(previous_flight["arrival_airport"] == flight["departure_airport"] and
                      (timestamp(flight["scheduled_departure"]) - timestamp(previous_flight["scheduled_arrival"])).total_seconds() >=
                      request["minimum_connection_minutes"] * 60, f"missed_connection:{identifier}")
            if index == len(request["bookings"]) - 1:
                check(timestamp(flight["scheduled_arrival"]) <= timestamp(request["final_deadline"]), f"final_deadline:{identifier}")
            previous_flight = flight
            ledger = [p for p in payments if p["booking_id"] == identifier and p["status"] == "completed"]
            gross = sum(amount(p["amount"]) for p in ledger if p["direction"] == "charge")
            refunded = sum(amount(p["amount"]) for p in ledger if p["direction"] == "refund")
            check(gross == amount(booking["paid_amount"]) and gross - refunded >= amount(booking["ticket_price"]) + amount(booking["change_fee"]), f"unsettled_ledger:{identifier}")
            check(bool(booking["seat_number"]) and any(s.get("booking_id") == identifier and s["flight_id"] == booking["flight_id"] for s in active["seat"].values()), f"missing_seat:{identifier}")
            for kind in ("baggage", "special_service"):
                originals = [r for r in original[kind].values() if r["booking_id"] == identifier]
                for old in originals:
                    pk = "baggage_id" if kind == "baggage" else "service_id"
                    row = active[kind].get(old[pk], {})
                    check(row.get("passenger_id") == request["passenger_id"] and row.get("flight_id") == booking["flight_id"]
                          and row.get("booking_revision") == booking["revision"] and row.get("status") not in {None, "cancelled"}, f"stale_service:{old[pk]}")
                    check(bool(row.get("transfer_history")) and row["transfer_history"][0] == {"flight_id": old["flight_id"], "booking_revision": 1}, f"missing_service_history:{old[pk]}")
            current = set()
            for key, clearance in world["connection_state"]["clearances"].items():
                if clearance["booking_id"] == identifier and clearance["passenger_id"] == request["passenger_id"] and clearance["leg"] == index + 1:
                    if clearance["snapshot"] == expected_snapshot(booking) and clearance["previous_clearance_id"] in previous_valid:
                        current.add(key)
            check(bool(current), f"no_current_clearance:{identifier}")
            valid_clearances[identifier] = sorted(current)
            previous_valid = current
            completed_bookings += len(errors) == error_count
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol == "skill_declaration_v1":
        from .declaration_runtime import declaration_state_clean
        if (public.get("format") != "skillhorizon.c09.freight_network.v2"
                or initial.get("format") != public["format"] or world.get("format") != public["format"]
                or public.get("runtime_identity") != "task_local_v1"
                or not all(declaration_state_clean(state) for state in (initial, world))):
            raise ValueError("Malformed declaration Freight state")
        observations = {"observed_skill_runs": None, "claimed_complete_skill_runs": None,
                        "skill_choices": None, "skill_observation_status": "controller_owned_unobserved"}
    elif protocol == "observational_v1":
        runs = world["connection_state"]["skill_runs"]
        observations = {"observed_skill_runs": len(runs),
                        "claimed_complete_skill_runs": sum(r["status"] == "claimed_complete" for r in runs.values()),
                        "skill_choices": dict(Counter(r["skill_id"] for r in runs.values()))}
    else:
        raise ValueError("Unknown Freight skill protocol")
    return {"success": not errors, "errors": errors, "completed_bookings": completed_bookings,
        "required_bookings": len(expected_booking_ids), "net_spend_usd": net_spend / 100,
        "valid_clearances": valid_clearances,
        **observations, "required_clearance_dependency_depth": max(len(r["bookings"]) for r in public["requests"]),
        "evidence_type": "zero_model_environment_evaluation_not_behavior"}


def evaluate(task, runtime=None):
    task = Path(task)
    try:
        return evaluate_world(read_json(task / "private/initial_obligations.json"), read_json(task / "checkpoint/world.json"),
                              read_json(Path(runtime or task / "runtime") / "world.json"))
    except (KeyError, ValueError, TypeError, ArithmeticError) as error:
        return {"success": False, "errors": [f"malformed_state:{type(error).__name__}:{error}"]}
