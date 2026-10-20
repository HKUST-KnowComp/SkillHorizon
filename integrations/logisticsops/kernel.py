"""Transactional airline operations over the existing C09 entity schema.

Background tables remain readable but immutable. Task-owned manifests are complete;
capacity is reserved by bookings, not by physical seat assignment. This module does
not evaluate task success or choose skills for an agent.
"""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import importlib
import json
from pathlib import Path
import sys
from unittest.mock import patch

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json
from .public_contract import REFUND_ENTITLEMENT


LIVE = {"confirmed", "checked_in"}
CLASSES = ("economy", "business", "first")
READS = {"search_flights", "search_bookings", "get_booking", "get_passenger",
         "get_flight_status", "get_seat_map", "get_baggage_info"}
REPLACED = {"create_booking", "assign_seat", "change_seat", "cancel_booking",
            "change_booking", "upgrade_seat_class", "process_payment", "process_refund",
            "check_in_passenger"}
NATIVE_WRITES = {"add_baggage", "request_special_service"}


class BusinessError(Exception):
    def __init__(self, code, details=None):
        super().__init__(code)
        self.details = {} if details is None else deepcopy(details)


def require(condition, code, details=None):
    if not condition:
        raise BusinessError(code, details)


def cents(value):
    try:
        amount = Decimal(str(value))
        require(amount.is_finite(), "INVALID_AMOUNT")
        scaled = amount * 100
        require(scaled == scaled.to_integral_value(), "USD_CENTS_REQUIRED")
        return int(scaled)
    except (InvalidOperation, ValueError, TypeError):
        raise BusinessError("INVALID_AMOUNT") from None


def dollars(value):
    return float(Decimal(value) / 100)


def success(data):
    return {"status": "success", "data": data, "message": ""}


def schema(properties, required=None):
    return {"type": "object", "properties": properties,
            "required": list(properties) if required is None else required, "additionalProperties": False}


def schema_error_details(error):
    path = "$/arguments" + "".join("/" + str(p).replace("~", "~0").replace("/", "~1")
                                  for p in error.absolute_path)
    details = {"path": path, "constraint": error.validator, "expected": error.validator_value}
    if error.validator in {"maxItems", "minItems", "maxLength", "minLength"}:
        details["actual_count"] = len(error.instance)
    elif error.validator == "required":
        details["missing_fields"] = [k for k in error.validator_value if k not in error.instance]
    elif error.validator == "additionalProperties":
        details["unexpected_fields"] = sorted(set(error.instance) - set(error.schema.get("properties", {})))
    elif error.validator == "type":
        details["actual_type"] = type(error.instance).__name__
    return details


class Airline:
    def __init__(self, source, runtime):
        self.source = Path(source).resolve()
        self.runtime = Path(runtime).resolve()
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Runtime must be separate from original assets")
        self.backend = load_backend(self.source)
        self.world = read_json(self.runtime / "world.json")
        state_type = importlib.import_module("mcp_servers.state.state_manager").StateManager
        db_type = importlib.import_module("mcp_servers.state.database").Database

        class MemoryDatabase(db_type):
            def _flush(self, entity_type):
                pass

            def _ensure_loaded(self, entity_type):
                canonical = self._normalize_entity_type(entity_type)
                if canonical not in self._data:
                    raise ValueError(f"Unadmitted entity table: {canonical}")
                return canonical

        class MemoryState(state_type):
            def _persist_counters(self):
                pass

        # Bypass the original disk constructor. A single atomic world snapshot is
        # authoritative, so a failed multi-table mutation cannot partially commit.
        self.sm = MemoryState.__new__(MemoryState)
        self.sm.db_path = str(self.runtime)
        self.sm._active_dir = str(self.runtime)
        self.sm.db = MemoryDatabase(str(self.runtime))
        self._bind()
        self.metadata = {name: deepcopy(meta) for name, meta in self.backend.TOOL_METADATA.items()
                         if name in READS | REPLACED | NATIVE_WRITES}
        for item in self.metadata.values():
            item["input_schema"]["additionalProperties"] = False
        for name in ("process_payment", "process_refund"):
            self.metadata[name]["input_schema"]["properties"]["method"]["enum"] = ["credit_card", "debit_card", "bank_transfer"]
        self.metadata["request_special_service"]["input_schema"]["required"].append("booking_id")
        self.metadata["request_special_service"]["input_schema"]["properties"]["booking_id"]["description"] = "Required booking to bind this service to an itinerary revision"
        text = {"type": "string", "minLength": 1}
        for kind in ("baggage", "special_service"):
            self.metadata[f"transfer_{kind}"] = {
                "description": f"Move an existing {kind} to its booking's current itinerary revision. Preserve its identity.",
                "input_schema": schema({f"{kind}_id": text, "booking_id": text,
                    "expected_revision": {"type": "integer", "minimum": 1}})}
        self.metadata["list_booking_services"] = {
            "description": "Read all baggage and special-service records attached to a booking, including stale links.",
            "input_schema": schema({"booking_id": text})}
        self.metadata["get_booking"]["description"] += " Includes itinerary revision and the actual payment ledger."
        self.metadata["process_refund"]["description"] = (
            "Refund a cancelled booking within its cancellation entitlement and original payment method; USD cents only. "
            "A live booking cannot be refunded. Cancellation cannot be undone or used to preserve a required original booking ID.")
        self.metadata["cancel_booking"]["description"] = (
            "Irreversibly cancel a booking and its baggage and special services. No public operation restores this booking ID. "
            "A new booking cannot satisfy a task obligation requiring the original ID. Cancellation records refund entitlement; "
            "it does not itself refund money. " + REFUND_ENTITLEMENT)
        self.metadata["change_booking"]["description"] = (
            "Rebook a confirmed booking on the same route. Reserve once, retain ledger, increment revision. "
            "Disrupted-flight changes waive fees; otherwise add USD 75. Ground services require explicit transfer. "
            "A cheaper flight does not automatically refund historical payments or accumulated fees; a cancelled booking cannot be rebooked.")
        self.validate()

    def _bind(self):
        self.sm.db._data = self.world["tables"]
        self.sm.db._id_counters = self.world.setdefault("id_counters", {
            name: len(rows) for name, rows in self.world["tables"].items()})
        self.sm._counters = self.world.setdefault("counters", {})

    def owned(self, entity_type, identifier):
        row = self.sm.get_entity(entity_type, identifier)
        require(row is not None, f"{entity_type.upper()}_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def live_booking(self, identifier):
        booking = self.owned("booking", identifier)
        require(booking["status"] in LIVE, "INVALID_BOOKING_STATUS")
        return booking

    def _now(self):
        base = datetime.fromisoformat(self.world["clock_origin"].replace("Z", "+00:00"))
        return (base + timedelta(seconds=self.world.get("sequence", 0))).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

    def _update(self, entity_type, identifier, values):
        return self.sm.update_entity(entity_type, identifier, values)

    def _insert(self, entity_type, values):
        return self.sm.add_entity(entity_type, {**values, "task_id": self.world["task_id"]})

    def _inventory(self, flight_id):
        flight = self.owned("flight", flight_id)
        bookings = [b for b in self.world["tables"]["booking"] if b.get("flight_id") == flight_id and b.get("status") in LIVE]
        available = {c: flight["total_seats"][c] - sum(b["booking_class"] == c for b in bookings) for c in CLASSES}
        require(all(n >= 0 for n in available.values()), "NO_SEATS_AVAILABLE")
        self._update("flight", flight_id, {"available_seats": available})

    def _release_seat(self, booking):
        if booking.get("seat_number"):
            rows = [s for s in self.world["tables"]["seat"] if s.get("flight_id") == booking["flight_id"]
                    and s.get("seat_number") == booking["seat_number"]]
            require(len(rows) == 1, "SEAT_IDENTITY_CORRUPT")
            self._update("seat", rows[0]["seat_id"], {"status": "available", "passenger_id": None, "booking_id": None})

    def _assign(self, booking, number):
        flight = self.owned("flight", booking["flight_id"])
        require(flight["status"] in {"scheduled", "boarding"}, "FLIGHT_NOT_ELIGIBLE")
        matches = [s for s in self.world["tables"]["seat"] if s.get("flight_id") == flight["flight_id"] and s.get("seat_number") == number]
        require(len(matches) == 1, "SEAT_NOT_FOUND")
        seat = matches[0]
        require(seat["status"] == "available", "SEAT_UNAVAILABLE")
        require(seat["seat_class"] == booking["booking_class"], "CLASS_MISMATCH")
        self._release_seat(booking)
        self._update("seat", seat["seat_id"], {"status": "occupied", "passenger_id": booking["passenger_id"], "booking_id": booking["booking_id"]})
        return self._update("booking", booking["booking_id"], {"seat_number": number, "updated_at": self._now()})

    def _ledger(self, booking_id):
        rows = [r for r in self.world["tables"]["payment"] if r.get("booking_id") == booking_id and r.get("status") == "completed"]
        gross = sum(cents(r["amount"]) for r in rows if r["direction"] == "charge")
        refunds = sum(cents(r["amount"]) for r in rows if r["direction"] == "refund")
        return rows, gross, refunds

    def _native(self, name, args):
        with ExitStack() as stack:
            for module_name, module in list(sys.modules.items()):
                if module_name.startswith("mcp_servers") and callable(getattr(module, "now_iso", None)):
                    stack.enter_context(patch.object(module, "now_iso", self._now))
            result = self.backend.call_tool(name, state_manager=self.sm, **args)["content"]
        if result.get("status") != "success" and result.get("message", "").startswith("TOOL_EXECUTION_ERROR"):
            raise RuntimeError(result["message"])
        require(result.get("status") == "success", result.get("message", "NATIVE_TOOL_ERROR"))
        return result["data"]

    def _scope(self, name, args):
        if name in READS or name == "list_booking_services":
            return
        for key, kind in (("booking_id", "booking"), ("passenger_id", "passenger"), ("flight_id", "flight"),
                          ("new_flight_id", "flight"), ("baggage_id", "baggage"), ("special_service_id", "special_service")):
            if args.get(key):
                self.owned(kind, args[key])

    def dispatch(self, name, args):
        self._scope(name, args)
        if name in READS:
            result = self._native(name, args)
            if name == "get_booking":
                row = self.sm.get_entity("booking", args["booking_id"])
                result["revision"] = row.get("revision")
                result["refund_entitlement"] = row.get("refund_entitlement")
                _, paid, refunded = self._ledger(args["booking_id"])
                result["balance_due"] = (0.0 if row["status"] == "cancelled" else
                    dollars(max(0, cents(row["ticket_price"]) + cents(row["change_fee"]) - paid + refunded)))
            return result
        if name == "list_booking_services":
            require(self.sm.get_entity("booking", args["booking_id"]), "BOOKING_NOT_FOUND")
            return {kind: [r for r in self.sm.list_entities(kind) if r.get("booking_id") == args["booking_id"]]
                    for kind in ("baggage", "special_service")}
        if name in NATIVE_WRITES:
            booking = self.live_booking(args.get("booking_id"))
            if name == "request_special_service":
                require(args["passenger_id"] == booking["passenger_id"] and args["flight_id"] == booking["flight_id"], "BOOKING_IDENTITY_MISMATCH")
            before = {kind: {r.get(f"{kind}_id", r.get("service_id")) for r in self.sm.list_entities(kind)}
                      for kind in ("baggage", "special_service")}
            result = self._native(name, args)
            kind = "baggage" if name == "add_baggage" else "special_service"
            pk = "baggage_id" if kind == "baggage" else "service_id"
            for row in self.sm.list_entities(kind):
                if row[pk] not in before[kind]:
                    self._update(kind, row[pk], {"task_id": self.world["task_id"], "booking_revision": booking["revision"]})
            return {**result, "booking_revision": booking["revision"]}
        if name.startswith("transfer_"):
            kind = name.removeprefix("transfer_")
            row = self.owned(kind, args[f"{kind}_id"])
            booking = self.live_booking(args["booking_id"])
            require(row["booking_id"] == booking["booking_id"] and row["passenger_id"] == booking["passenger_id"], "SERVICE_IDENTITY_MISMATCH")
            require(args["expected_revision"] == booking["revision"], "STALE_ITINERARY")
            require(self.owned("flight", booking["flight_id"])["status"] in {"scheduled", "boarding"}, "FLIGHT_NOT_ELIGIBLE")
            require(row.get("status") not in {"cancelled", "completed", "claimed", "lost"}, "SERVICE_NOT_TRANSFERABLE")
            pk = "baggage_id" if kind == "baggage" else "service_id"
            require(row.get("booking_revision") != booking["revision"] or row["flight_id"] != booking["flight_id"], "ALREADY_CURRENT")
            history = row.get("transfer_history", []) + [{"flight_id": row["flight_id"], "booking_revision": row.get("booking_revision")}]
            return self._update(kind, row[pk], {"flight_id": booking["flight_id"], "booking_revision": booking["revision"], "transfer_history": history})
        return getattr(self, name)(**args)

    def create_booking(self, passenger_id, flight_id, booking_class, booking_channel):
        passenger, flight = self.owned("passenger", passenger_id), self.owned("flight", flight_id)
        require(passenger["status"] == "active", "PASSENGER_INACTIVE")
        require(flight["status"] == "scheduled", "FLIGHT_NOT_BOOKABLE")
        require(flight["available_seats"][booking_class] > 0, "NO_SEATS_AVAILABLE")
        require(not any(b.get("passenger_id") == passenger_id and b.get("flight_id") == flight_id and b.get("status") in LIVE
                        for b in self.sm.list_entities("booking")), "DUPLICATE_BOOKING")
        row = self._insert("booking", {"passenger_id": passenger_id, "flight_id": flight_id, "booking_class": booking_class,
            "booking_channel": booking_channel, "status": "confirmed", "seat_number": None, "checked_in": False,
            "ticket_price": flight["base_price"][booking_class], "change_fee": 0, "paid_amount": 0,
            "created_at": self._now(), "updated_at": self._now(), "revision": 1, "itinerary_history": []})
        self._inventory(flight_id)
        return row

    def assign_seat(self, booking_id, seat_number):
        booking = self.live_booking(booking_id)
        require(not booking.get("seat_number"), "SEAT_ALREADY_ASSIGNED")
        return self._assign(booking, seat_number)

    def change_seat(self, booking_id, new_seat_number):
        booking = self.live_booking(booking_id)
        require(booking.get("seat_number"), "NO_SEAT_ASSIGNED")
        require(booking["seat_number"] != new_seat_number, "SAME_SEAT")
        return self._assign(booking, new_seat_number)

    def change_booking(self, booking_id, new_flight_id, new_booking_class=None):
        booking = self.live_booking(booking_id)
        old = self.owned("flight", booking["flight_id"])
        target = self.owned("flight", new_flight_id)
        cabin = new_booking_class or booking["booking_class"]
        require(new_flight_id != old["flight_id"], "SAME_FLIGHT")
        require(old["status"] not in {"departed", "arrived"}, "FLIGHT_ALREADY_DEPARTED")
        require(target["status"] == "scheduled", "FLIGHT_NOT_BOOKABLE")
        require((old["departure_airport"], old["arrival_airport"]) == (target["departure_airport"], target["arrival_airport"]), "ROUTE_MISMATCH")
        require(target["available_seats"][cabin] > 0, "NO_SEATS_AVAILABLE")
        require(not any(b["booking_id"] != booking_id and b.get("passenger_id") == booking["passenger_id"]
                        and b.get("flight_id") == new_flight_id and b.get("status") in LIVE for b in self.sm.list_entities("booking")), "DUPLICATE_BOOKING")
        self._release_seat(booking)
        fee = 0 if old["status"] in {"cancelled", "delayed"} else 7500
        history = booking["itinerary_history"] + [{k: booking[k] for k in ("revision", "flight_id", "booking_class", "ticket_price", "change_fee")}]
        row = self._update("booking", booking_id, {"flight_id": new_flight_id, "booking_class": cabin, "seat_number": None,
            "checked_in": False, "status": "confirmed", "ticket_price": target["base_price"][cabin], "change_fee": dollars(cents(booking["change_fee"]) + fee),
            "revision": booking["revision"] + 1, "itinerary_history": history, "updated_at": self._now()})
        self._inventory(old["flight_id"])
        self._inventory(new_flight_id)
        return row

    def upgrade_seat_class(self, booking_id, target_class, new_seat_number=None):
        booking = self.live_booking(booking_id)
        flight = self.owned("flight", booking["flight_id"])
        require(flight["status"] in {"scheduled", "boarding"}, "FLIGHT_NOT_ELIGIBLE")
        require(CLASSES.index(target_class) > CLASSES.index(booking["booking_class"]), "INVALID_TARGET_CLASS")
        require(flight["available_seats"][target_class] > 0, "NO_SEATS_AVAILABLE")
        self._release_seat(booking)
        history = booking["itinerary_history"] + [{k: booking[k] for k in ("revision", "flight_id", "booking_class", "ticket_price", "change_fee")}]
        row = self._update("booking", booking_id, {"booking_class": target_class, "seat_number": None,
            "ticket_price": flight["base_price"][target_class], "status": "confirmed", "checked_in": False,
            "revision": booking["revision"] + 1, "itinerary_history": history, "updated_at": self._now()})
        if new_seat_number:
            row = self._assign(row, new_seat_number)
        self._inventory(flight["flight_id"])
        return row

    def cancel_booking(self, booking_id, reason=None):
        booking = self.live_booking(booking_id)
        flight = self.owned("flight", booking["flight_id"])
        require(flight["status"] not in {"departed", "arrived"}, "FLIGHT_ALREADY_DEPARTED")
        _, gross, _ = self._ledger(booking_id)
        percent = 100 if flight["status"] == "cancelled" else (80 if booking["status"] == "confirmed" else 50)
        entitlement = gross * percent // 100
        self._release_seat(booking)
        row = self._update("booking", booking_id, {"status": "cancelled", "seat_number": None, "checked_in": False,
            "refund_entitlement": dollars(entitlement), "cancellation_reason": reason,
            "cancellation_basis": {"flight_status": flight["status"], "booking_status": booking["status"], "percent": percent},
            "updated_at": self._now()})
        for kind, pk in (("baggage", "baggage_id"), ("special_service", "service_id")):
            for item in self.sm.list_entities(kind):
                if item.get("booking_id") == booking_id:
                    self._update(kind, item[pk], {"status": "cancelled", "cancelled_at": self._now()})
        self._inventory(flight["flight_id"])
        return {**row, "refund_amount": dollars(entitlement)}

    def _payment(self, booking, amount, method, direction, reason=None):
        rows, _, _ = self._ledger(booking["booking_id"])
        sequence = len(self.sm.list_entities("payment")) + 1
        return self._insert("payment", {"booking_id": booking["booking_id"], "amount": dollars(amount), "currency": "USD",
            "method": method, "direction": direction, "status": "completed", "booking_revision": booking["revision"],
            "reference_number": f"C09-{sequence:07d}", "processed_at": self._now(), "created_at": self._now(), "reason": reason,
            "prior_payment_ids": [p["payment_id"] for p in rows]})

    def process_payment(self, booking_id, amount, method, currency="USD"):
        booking = self.live_booking(booking_id)
        require(currency == "USD", "CURRENCY_NOT_SUPPORTED")
        require(method in {"credit_card", "debit_card", "bank_transfer"}, "PAYMENT_METHOD_NOT_ADMITTED")
        value = cents(amount)
        require(value > 0, "INVALID_AMOUNT")
        _, paid, refunded = self._ledger(booking_id)
        due = cents(booking["ticket_price"]) + cents(booking["change_fee"]) - paid + refunded
        require(value <= due, "OVERPAYMENT")
        row = self._payment(booking, value, method, "charge")
        self._update("booking", booking_id, {"paid_amount": dollars(paid + value), "updated_at": self._now()})
        return {**row, "balance_due": dollars(due - value)}

    def process_refund(self, booking_id, refund_amount, method, reason=None):
        booking = self.owned("booking", booking_id)
        require(booking["status"] == "cancelled", "INVALID_BOOKING_STATUS")
        require(method in {"credit_card", "debit_card", "bank_transfer"}, "PAYMENT_METHOD_NOT_ADMITTED")
        amount = cents(refund_amount)
        require(amount > 0, "INVALID_AMOUNT")
        rows, _, refunded = self._ledger(booking_id)
        remaining = cents(booking["refund_entitlement"]) - refunded
        require(amount <= remaining, "REFUND_EXCEEDS_ENTITLEMENT")
        method_balance = sum((1 if r["direction"] == "charge" else -1) * cents(r["amount"]) for r in rows if r["method"] == method)
        require(amount <= method_balance, "REFUND_METHOD_MISMATCH")
        return {**self._payment(booking, amount, method, "refund", reason), "remaining_refundable": dollars(remaining - amount)}

    def check_in_passenger(self, booking_id):
        booking = self.live_booking(booking_id)
        require(booking["status"] == "confirmed", "ALREADY_CHECKED_IN")
        require(self.owned("flight", booking["flight_id"])["status"] in {"scheduled", "boarding"}, "FLIGHT_NOT_ELIGIBLE")
        require(booking.get("seat_number"), "NO_SEAT_ASSIGNED")
        _, paid, refunded = self._ledger(booking_id)
        require(paid - refunded >= cents(booking["ticket_price"]) + cents(booking["change_fee"]), "OUTSTANDING_BALANCE")
        for kind in ("baggage", "special_service"):
            require(all(r["flight_id"] == booking["flight_id"] and r.get("booking_revision") == booking["revision"]
                        for r in self.sm.list_entities(kind) if r.get("booking_id") == booking_id and r.get("status") != "cancelled"), "STALE_GROUND_SERVICE")
        return self._update("booking", booking_id, {"status": "checked_in", "checked_in": True, "updated_at": self._now()})

    def validate(self):
        task_id = self.world["task_id"]
        tables = self.world["tables"]
        bookings = [b for b in tables["booking"] if b.get("task_id") == task_id]
        require(len({b["booking_id"] for b in bookings}) == len(bookings), "DUPLICATE_BOOKING_ID")
        live_keys = [(b["passenger_id"], b["flight_id"]) for b in bookings if b["status"] in LIVE]
        require(len(set(live_keys)) == len(live_keys), "DUPLICATE_BOOKING")
        for flight in tables["flight"]:
            if flight.get("task_id") != task_id:
                continue
            seats = [s for s in tables["seat"] if s["flight_id"] == flight["flight_id"]]
            require(len({s["seat_number"] for s in seats}) == len(seats), "DUPLICATE_PHYSICAL_SEAT")
            for cabin in CLASSES:
                capacity = flight["total_seats"][cabin]
                require(cents(flight["base_price"][cabin]) >= 0, "INVALID_FARE")
                held = sum(b["flight_id"] == flight["flight_id"] and b["booking_class"] == cabin and b["status"] in LIVE for b in bookings)
                require(type(capacity) is int and capacity >= held, "RESERVATION_CAPACITY")
                require(sum(s["seat_class"] == cabin for s in seats) == capacity, "INCOMPLETE_ACTIVE_MANIFEST")
                require(flight["available_seats"][cabin] == capacity - held, "INVENTORY_CONSERVATION")
            for seat in seats:
                owners = [b for b in bookings if b["status"] in LIVE and b["flight_id"] == flight["flight_id"] and b.get("seat_number") == seat["seat_number"]]
                require(len(owners) <= 1, "DUPLICATE_SEAT_OWNER")
                if owners:
                    owner = owners[0]
                    require(seat["status"] == "occupied" and seat.get("booking_id") == owner["booking_id"]
                            and seat.get("passenger_id") == owner["passenger_id"] and seat["seat_class"] == owner["booking_class"], "SEAT_IDENTITY_MISMATCH")
                else:
                    require(seat["status"] == "available" and not seat.get("passenger_id") and not seat.get("booking_id"), "ORPHAN_SEAT_OWNER")
        for booking in bookings:
            self.owned("flight", booking["flight_id"])
            self.owned("passenger", booking["passenger_id"])
            require(cents(booking["ticket_price"]) >= 0 and cents(booking["change_fee"]) >= 0, "INVALID_FARE")
            require(type(booking["revision"]) is int and booking["revision"] >= 1, "INVALID_REVISION")
            rows, paid, refunded = self._ledger(booking["booking_id"])
            require(paid == cents(booking["paid_amount"]), "PAID_LEDGER_MISMATCH")
            require(all(r["currency"] == "USD" and cents(r["amount"]) > 0 for r in rows), "INVALID_PAYMENT_LEDGER")
            require(0 <= refunded <= paid, "REFUND_CONSERVATION")
            if booking["status"] == "cancelled":
                require(not booking.get("seat_number") and refunded <= cents(booking["refund_entitlement"]), "CANCELLATION_CONSERVATION")
            if booking.get("seat_number") and booking["status"] in LIVE:
                require(any(s["flight_id"] == booking["flight_id"] and s["seat_number"] == booking["seat_number"] for s in tables["seat"]), "SEAT_NOT_FOUND")
        for payment in tables["payment"]:
            if payment.get("task_id") == task_id:
                self.owned("booking", payment["booking_id"])
                require(payment["direction"] in {"charge", "refund"} and payment["method"] in {"credit_card", "debit_card", "bank_transfer"}, "INVALID_PAYMENT_LEDGER")
        for kind in ("baggage", "special_service"):
            for row in tables[kind]:
                if row.get("task_id") == task_id:
                    owner = self.owned("booking", row["booking_id"])
                    require(row["passenger_id"] == owner["passenger_id"], "SERVICE_IDENTITY_MISMATCH")

    def call(self, name, arguments):
        before = deepcopy(self.world)
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except jsonschema.ValidationError as error:
                raise BusinessError("ARGUMENT_SCHEMA", schema_error_details(error)) from None
            except (ValueError, TypeError):
                raise BusinessError("ARGUMENT_SCHEMA", {
                    "path": "$/arguments", "constraint": "finite_json",
                    "message": "Arguments must be finite JSON values."}) from None
            data = self.dispatch(name, deepcopy(arguments))
            if name in REPLACED | NATIVE_WRITES | {"transfer_baggage", "transfer_special_service"}:
                self.validate()
            result = success(data)
        except BusinessError as error:
            self.world = before
            self._bind()
            result = {"status": "error", "data": error.details, "message": str(error)}
        except Exception:
            self.world = before
            self._bind()
            raise
        self.world["sequence"] = self.world.get("sequence", 0) + 1
        try:
            self.record_call(name, arguments, result)
            write_json(self.runtime / "world.json", self.world)
        except Exception:
            self.world = before
            self._bind()
            raise
        return result

    def record_call(self, name, arguments, result):
        """Optional observational bookkeeping before the atomic state commit."""
