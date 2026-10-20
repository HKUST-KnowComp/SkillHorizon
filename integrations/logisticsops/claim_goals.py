"""Read-only current local goals on one exactly replayed Freight dispatch cut."""

from copy import deepcopy

from .declaration_build import LOCAL_GOALS
from .kernel import BusinessError, cents


COGNITIVE = {"assess-disruption", "plan-reaccommodation"}
BOOKING_GOALS = {"rebook-itinerary", "reconcile-ground-services", "settle-travel-account"}
RECORD_GOALS = {
    "complete-passenger-handoff": ("clearance", "current_clearance"),
    "release-through-baggage": ("baggage_release", "current_release"),
    "schedule-cross-cohort-transfer": ("airport_transfer", "current_trip"),
    "forward-through-baggage": ("baggage_forwarding", "current_forwarding"),
}
TABLE_KEYS = {"booking": "booking_id", "payment": "payment_id", "baggage": "baggage_id",
              "special_service": "service_id"}


def clause(rule, passed):
    return {"rule": rule, "passed": passed}


class C09Goals:
    def __init__(self, runtime, initial):
        self.runtime, self.initial = runtime, initial

    def objects(self, kind):
        if kind in TABLE_KEYS:
            return {row[TABLE_KEYS[kind]]: row for row in self.runtime.world["tables"][kind]}
        if kind == "clearance":
            return self.runtime.state["clearances"]
        table = {"baggage_release": "releases", "airport_transfer": "trips",
                 "baggage_forwarding": "forwardings"}.get(kind)
        return self.runtime.network[table] if table else None

    def references(self, refs, checks):
        resolved = []
        if not isinstance(refs, list):
            raise ValueError("Validated Freight references must be a list")
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                    or not all(isinstance(value, str) and value for value in ref.values())):
                raise ValueError("Freight references require kind/id strings")
            kind, key = ref["kind"], ref["id"]
            rows = self.objects(kind)
            checks.append(clause("REFERENCE_KIND", rows is not None))
            if rows is None:
                continue
            row = rows.get(key)
            checks.append(clause("REFERENCE_EXISTS", row is not None))
            if row is not None:
                if kind in TABLE_KEYS:
                    checks.append(clause("REFERENCE_TASK", row.get("task_id") == self.runtime.world["task_id"]))
                resolved.append((kind, key, row))
        return resolved

    def checked(self, rule, function, checks):
        try:
            function()
            checks.append(clause(rule, True))
        except BusinessError as error:
            checks.append(clause(rule + ":" + str(error), False))

    def booking_goal(self, skill, key, checks):
        runtime = self.runtime
        booking = runtime.owned("booking", key)
        originals = {b["booking_id"]: b for b in self.initial["tables"]["booking"]
                     if b.get("task_id") == runtime.world["task_id"]}
        original = originals.get(key)
        checks.append(clause("ORIGINAL_BOOKING_IDENTITY", original is not None
                             and original["passenger_id"] == booking["passenger_id"]))
        if original is None:
            return
        if skill == "rebook-itinerary":
            flight = runtime.owned("flight", booking["flight_id"])
            old_flight = runtime.owned("flight", original["flight_id"])
            checks.extend([
                clause("ELIGIBLE_BOOKED_FLIGHT", booking["status"] in {"confirmed", "checked_in"}
                       and flight["status"] in {"scheduled", "boarding"}),
                clause("ORIGINAL_ROUTE", all(flight[k] == old_flight[k]
                       for k in ("departure_airport", "arrival_airport"))),
                clause("ACTUAL_OWNED_SEAT", bool(booking.get("seat_number")) and any(
                    seat["flight_id"] == booking["flight_id"] and seat["seat_number"] == booking["seat_number"]
                    and seat["seat_class"] == booking["booking_class"] and seat["status"] == "occupied"
                    and seat.get("booking_id") == key and seat.get("passenger_id") == booking["passenger_id"]
                    for seat in runtime.world["tables"]["seat"])),
            ])
        elif skill == "reconcile-ground-services":
            checks.append(clause("LIVE_BOOKING", booking["status"] in {"confirmed", "checked_in"}))
            for kind in ("baggage", "special_service"):
                rows = self.objects(kind)
                for old in self.initial["tables"][kind]:
                    if old.get("booking_id") != key or old.get("task_id") != runtime.world["task_id"]:
                        continue
                    row = rows.get(old[TABLE_KEYS[kind]])
                    current = row is not None and row.get("booking_id") == key and all(
                        row.get(field) == value for field, value in {
                            "passenger_id": booking["passenger_id"], "flight_id": booking["flight_id"],
                            "booking_revision": booking["revision"]}.items())
                    checks.append(clause("ORIGINAL_CURRENT_" + kind.upper(), current
                                         and row.get("status") not in {None, "cancelled", "lost"}))
                    changed = (old["flight_id"], old.get("booking_revision")) != (
                        booking["flight_id"], booking["revision"])
                    checks.append(clause("PRESERVED_" + kind.upper() + "_HISTORY", not changed or
                        (row is not None and {"flight_id": old["flight_id"],
                         "booking_revision": old.get("booking_revision")} in row.get("transfer_history", []))))
        else:
            rows, paid, refunded = runtime._ledger(key)
            checks.append(clause("ACTUAL_LEDGER_OWNERSHIP", all(
                p.get("task_id") == runtime.world["task_id"] and p["booking_id"] == key
                and p["currency"] == "USD" and p["method"] in {"credit_card", "debit_card", "bank_transfer"}
                for p in rows)))
            settled = (refunded == cents(booking["refund_entitlement"]) if booking["status"] == "cancelled"
                       else booking["status"] in {"confirmed", "checked_in"}
                       and paid - refunded >= cents(booking["ticket_price"]) + cents(booking["change_fee"]))
            checks.append(clause("CURRENT_BALANCE_SETTLED", settled))
            net = sum((1 if p["direction"] == "charge" else -1) * cents(p["amount"])
                      for p in runtime.world["tables"]["payment"]
                      if p.get("task_id") == runtime.world["task_id"] and p["status"] == "completed")
            checks.append(clause("SHARED_SPEND_LIMIT", net <= cents(runtime.public["max_net_spend_usd"])))

    def assess(self, claim):
        skill = claim["skill_id"]
        if skill not in LOCAL_GOALS:
            raise ValueError("Unknown Freight skill")
        checks = []
        scope = self.references(claim.get("scope", []), checks)
        outputs = self.references(claim.get("outputs", []), checks)
        result = {**deepcopy(claim), "process_adherence": "unobserved"}
        if skill in COGNITIVE:
            return {**result, "goal_verdict": "unknown", "clauses": checks,
                    "reason": "UNDERSTANDING_OR_PLAN_UNOBSERVED"}

        if skill in BOOKING_GOALS:
            allowed = {"booking"} | ({"payment"} if skill == "settle-travel-account" else
                      {"baggage", "special_service"} if skill == "reconcile-ground-services" else set())
            for kind, key, row in scope + outputs:
                checks.append(clause("REFERENCE_KIND_FOR_GOAL", kind in allowed))
                if kind == "payment" and kind in allowed:
                    checks.append(clause("EXPLICIT_COMPLETED_PAYMENT", row.get("status") == "completed"))
                if kind in {"baggage", "special_service"} and kind in allowed:
                    checks.append(clause("EXPLICIT_ORIGINAL_SERVICE", any(
                        old[TABLE_KEYS[kind]] == key and old.get("task_id") == self.runtime.world["task_id"]
                        for old in self.initial["tables"][kind])))
            candidates = [key if kind == "booking" else row["booking_id"]
                          for kind, key, row in scope + outputs if kind in allowed]
            targets = set(candidates)
            anchors = {key for kind, key, _ in scope if kind == "booking"}
            if len(anchors) == 1:
                for kind, key, row in outputs:
                    if kind in allowed:
                        checks.append(clause("OUTPUT_TARGET", (key if kind == "booking" else row["booking_id"]) in anchors))
            explicit = {key if kind == "booking" else row["booking_id"]
                        for kind, key, row in outputs if kind in allowed}
            for key in sorted(explicit):
                self.checked("EXPLICIT_BOOKING_GOAL", lambda key=key: self.booking_goal(skill, key, checks), checks)
            if len(targets) == 1 and not explicit:
                key = next(iter(targets))
                self.checked("BOOKING_GOAL", lambda: self.booking_goal(skill, key, checks), checks)
        else:
            kind, method = RECORD_GOALS[skill]
            allowed_scope = {kind, "booking"} if kind == "clearance" else {kind}
            for ref_kind, _, _ in scope:
                checks.append(clause("SCOPE_KIND_FOR_GOAL", ref_kind in allowed_scope))
            for ref_kind, _, _ in outputs:
                checks.append(clause("OUTPUT_KIND_FOR_GOAL", ref_kind == kind))
            selected = list({key: row for ref_kind, key, row in scope + outputs if ref_kind == kind}.items())
            # A booking can locate its clearance without inventing a new artifact.
            bookings = {key for ref_kind, key, _ in scope if ref_kind == "booking"}
            if len(bookings) > 1:
                checks.append(clause("UNIQUE_CLEARANCE_BOOKING_TARGET", None))
            if not selected and kind == "clearance" and len(bookings) == 1:
                selected = [(key, row) for key, row in self.objects(kind).items() if row["booking_id"] in bookings]
                valid = []
                for key, row in selected:
                    try:
                        getattr(self.runtime, method)(key)
                        valid.append((key, row))
                    except BusinessError:
                        pass
                selected = valid
                checks.append(clause("CURRENT_CLEARANCE_EXISTS", bool(selected)))
            anchors = {key for ref_kind, key, _ in scope if ref_kind == kind}
            for key, row in selected:
                self.checked("CURRENT_" + kind.upper(), lambda key=key: getattr(self.runtime, method)(key), checks)
                if len(anchors) == 1:
                    checks.append(clause("OUTPUT_TARGET", key in anchors))
                if kind == "clearance" and bookings:
                    checks.append(clause("CLEARANCE_BOOKING", row["booking_id"] in bookings))
            targets = {key for key, _ in selected}

        if len(targets) != 1:
            checks.append(clause("UNIQUE_GOAL_TARGET", None))
        verdict = ("contradicted" if any(c["passed"] is False for c in checks) else
                   "unknown" if any(c["passed"] is None for c in checks) or not checks else "supported")
        return {**result, "clauses": checks, "goal_verdict": verdict,
                "reason": {"supported": "CURRENT_LOCAL_GOAL_SATISFIED",
                           "contradicted": "LOCAL_GOAL_CONTRADICTED",
                           "unknown": "TARGET_UNRESOLVED"}[verdict]}
