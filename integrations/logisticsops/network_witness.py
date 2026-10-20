"""One feasible network policy, using public tools and actual recorded outputs only."""

from datetime import datetime, timedelta
import math
from integrations.internal_mcp.reference_reads import ImmutableSkillDocuments


PROTOCOL = "skill_declaration_v1"


class WitnessCallLimit(BaseException):
    def __init__(self, calls, max_calls):
        self.calls = self.witness_calls = calls
        self.max_calls = max_calls
        super().__init__(f"C09 witness call limit reached: {calls}/{max_calls}")


class _CountedCaller:
    def __init__(self, invoke, max_calls):
        self.invoke, self.max_calls, self.calls = invoke, max_calls, 0
        self._freight_counter = self

    async def __call__(self, name, **arguments):
        if self.calls >= self.max_calls:
            raise WitnessCallLimit(self.calls, self.max_calls)
        self.calls += 1
        try:
            return await self.invoke(name, **arguments)
        except BaseException as error:
            error.witness_calls = self.calls
            raise


def _protocol_caller(call, skill_protocol, max_calls):
    if skill_protocol is None:
        if max_calls is not None:
            raise ValueError("max_calls requires an explicit declaration protocol")
        return call, None
    if skill_protocol != PROTOCOL or type(max_calls) is not int or max_calls < 1:
        raise ValueError("Declaration Freight requires its protocol and a finite positive call limit")
    counter = getattr(call, "_freight_counter", None)
    if counter is not None:
        if not isinstance(counter, _CountedCaller) or counter.max_calls != max_calls:
            raise ValueError("Freight witness layers must share one call budget")
        return call, counter
    counter = _CountedCaller(call, max_calls)
    return counter, counter


def instant(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


async def run(call, *, alternative=False, use_script=True, stop_after_legs=None, stop_at=None,
              freight=None, skill_protocol=None, max_calls=None, efficient_reads=False, load_order=False,
              demand_flights=False):
    call, counter = _protocol_caller(call, skill_protocol, max_calls)
    documents = freight.documents if freight is not None else ImmutableSkillDocuments()
    public = await call("read_task")
    if counter is not None and (public.get("format") != "skillhorizon.c09.freight_network.v2"
            or public.get("runtime_identity") != "task_local_v1"
            or public.get("policy", {}).get("skill_protocol") != PROTOCOL
            or freight is None or freight.skill_protocol != PROTOCOL):
        raise ValueError("Declaration witness requires the public Freight V2 identity and policy")
    network = await call("read_transfer_network")
    people = {r["passenger_id"]: r for r in public["requests"]}
    flights = {} if demand_flights else {fid: await call("get_flight_status", flight_id=fid)
        for route in public["route"] for fid in route["alternative_flight_ids"]}
    clearances, releases, trips = {}, {}, {}
    history, all_releases, all_trips = {}, {}, {}
    offset = 0
    while True:
        page = await call("list_clearances", offset=offset, limit=30)
        history.update({c["clearance_id"]: c for c in page["records"]})
        offset += len(page["records"])
        if offset >= page["total"]:
            break
    for row in network["releases"]:
        release = await call("read_baggage_release", release_id=row["release_id"])
        all_releases[release["release_id"]] = release
    for row in network["trips"]:
        trip = await call("read_airport_transfer", trip_id=row["trip_id"])
        all_trips[trip["trip_id"]] = trip
    if freight is not None:
        await freight.load(call, network)

    # Repair the explicit public dependency closure, not every previously completed booking.
    invalid = {t["trip_id"] for t in all_trips.values() if t["status"] != "scheduled"}
    while True:
        expanded = invalid | {c["clearance_id"] for c in history.values() if c.get("transfer_trip_id") in invalid or c["previous_clearance_id"] in invalid}
        expanded |= {r["release_id"] for r in all_releases.values() if r["origin_clearance_id"] in invalid}
        expanded |= {t["trip_id"] for t in all_trips.values() if set(t["inbound_clearance_ids"] + t["baggage_release_ids"] + t.get("baggage_forwarding_ids", [])) & invalid or t.get("previous_vehicle_trip_id") in invalid}
        if freight is not None:
            expanded |= freight.invalid_dependents(invalid)
        if expanded == invalid:
            break
        invalid = expanded
    if freight is not None:
        await freight.cancel_invalid(call, invalid)
    for trip in all_trips.values():
        if trip["trip_id"] in invalid and trip["status"] == "scheduled":
            await call("cancel_airport_transfer", trip_id=trip["trip_id"])
    clearances = {c["booking_id"]: c for c in history.values() if c["clearance_id"] not in invalid}
    releases = {r["obligation_id"]: r for r in all_releases.values() if r["release_id"] not in invalid}
    trips = {t["group_id"]: t for t in all_trips.values() if t["trip_id"] not in invalid}
    if demand_flights:
        for identifier in {c["snapshot"]["booking"]["flight_id"] for c in history.values()}:
            flights[identifier] = await call("get_flight_status", flight_id=identifier)

    async def begin(skill, bookings=()):
        await documents.read(call, skill)
        if counter is not None:
            return None
        return (await call("begin_skill", skill_id=skill, booking_ids=list(bookings)))["run_id"]

    async def finish(skill, outputs=()):
        if counter is None:
            await call("finish_skill", run_id=skill, output_ids=list(outputs))

    for index, route in enumerate(public["route"]):
        if stop_after_legs is not None and index >= stop_after_legs:
            return
        if index:
            if freight is not None:
                await freight.advance(call, network, releases, clearances, flights, index, alternative=alternative)
            groups = [g for g in network["groups"] if g["leg"] == index + 1]
            def group_priority(group):
                due = [o for o in network["baggage_obligations"] if o["delivery_leg"] == index + 1
                       and o["passenger_id"] in group["passenger_ids"]]
                weight = sum(releases[o["obligation_id"]]["bag"]["weight_kg"] for o in due)
                return (min(people[p]["final_deadline"] for p in group["passenger_ids"]),
                        -weight if load_order else 0, group["group_id"])
            groups.sort(key=group_priority)
            for group in groups:
                if group["group_id"] in trips:
                    continue
                skill = await begin("schedule-cross-cohort-transfer", [people[p]["bookings"][index] for p in group["passenger_ids"]])
                inbound = [clearances[people[p]["bookings"][index - 1]] for p in group["passenger_ids"]]
                for clearance in inbound:
                    await call("read_clearance", clearance_id=clearance["clearance_id"])
                due = [o for o in network["baggage_obligations"] if o["delivery_leg"] == index + 1 and o["passenger_id"] in group["passenger_ids"]]
                cargo = [await call("read_baggage_release", release_id=releases[o["obligation_id"]]["release_id"]) for o in due]
                weight = sum(r["bag"]["weight_kg"] for r in cargo)
                chairs = sum("wheelchair" in people[p]["required_ground_services"] for p in group["passenger_ids"])
                ready = [instant(flights[c["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) for c in inbound]
                for release in cargo:
                    origin = clearances[release["origin_booking_id"]]
                    ready.append(instant(flights[origin["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) + timedelta(minutes=release["handling_minutes"]))
                if freight is not None:
                    forwarding_ids, arrivals = freight.ground_bindings(cargo, index, network)
                    ready.extend(arrivals)
                choices = []
                for vehicle in network["vehicles"]:
                    if vehicle["leg"] != group["leg"] or weight > vehicle["weight_capacity_kg"] or chairs > vehicle["wheelchair_capacity"]:
                        continue
                    prior = [t for t in trips.values() if t["vehicle_id"] == vehicle["vehicle_id"]]
                    previous = prior[-1] if prior else None
                    departure = max(ready + ([instant(previous["arrival_time"]) + timedelta(minutes=vehicle["turnaround_minutes"])] if previous else []))
                    duration = vehicle["base_travel_minutes"] + math.ceil(weight / vehicle["weight_step_kg"]) * vehicle["minutes_per_weight_step"] + chairs * vehicle["wheelchair_minutes"]
                    arrival = departure + timedelta(minutes=duration)
                    choices.append((arrival, vehicle["vehicle_id"], departure, previous))
                choices.sort()
                choice = choices[1] if alternative and len(choices) > 1 and choices[1][0] <= choices[0][0] + timedelta(minutes=10) else choices[0]
                arrival, vehicle_id, departure, previous = choice
                args = {"group_id": group["group_id"], "vehicle_id": vehicle_id, "inbound_clearance_ids": [c["clearance_id"] for c in inbound],
                    "baggage_release_ids": [r["release_id"] for r in cargo], "departure_time": departure.isoformat(), "arrival_time": arrival.isoformat(), "weight_kg": weight, "wheelchair_count": chairs}
                if previous:
                    args["previous_vehicle_trip_id"] = previous["trip_id"]
                if freight is not None:
                    args["baggage_forwarding_ids"] = forwarding_ids
                trip = await call("schedule_airport_transfer", **args)
                trips[group["group_id"]] = trip
                await finish(skill, [trip["trip_id"]])
            if stop_at == "transfers" and index == 1:
                return
        ordered = sorted(public["requests"], key=lambda r: (r["final_deadline"], r["passenger_id"]))
        if alternative:
            ordered = sorted(ordered, key=lambda r: r["final_deadline"])
        for request in ordered:
            booking_id = request["bookings"][index]
            if booking_id in clearances:
                continue
            assessment = await begin("assess-disruption", [booking_id])
            booking = await call("get_booking", booking_id=booking_id)
            services = await call("list_booking_services", booking_id=booking_id)
            await finish(assessment)
            plan = await begin("plan-reaccommodation", [booking_id])
            candidates = []
            group = next((g for g in network["groups"] if g["leg"] == index + 1 and request["passenger_id"] in g["passenger_ids"]), None)
            trip = trips[group["group_id"]] if group else None
            identifiers = route["alternative_flight_ids"]
            if demand_flights:
                schedules = {f["flight_id"]: f for f in route["alternative_schedules"]}
                identifiers = sorted(identifiers, key=lambda fid: (schedules[fid]["scheduled_departure"], fid))
            for identifier in identifiers:
                flight = await call("get_flight_status", flight_id=identifier)
                flights[identifier] = flight
                if flight["available_seats"]["economy"] <= 0 and booking["flight_id"] != identifier:
                    continue
                if trip and instant(trip["arrival_time"]) + timedelta(minutes=group["boarding_buffer_minutes"]) > instant(flight["scheduled_departure"]):
                    continue
                if index == len(public["route"]) - 1 and instant(flight["scheduled_arrival"]) > instant(request["final_deadline"]):
                    continue
                candidates.append(flight)
                if demand_flights:
                    break
            if not candidates:
                raise ValueError("No feasible public onward flight")
            chosen = min(candidates, key=lambda f: (f["scheduled_departure"], f["flight_id"]))
            if use_script and index == len(public["route"]) - 1:
                selected = [clearances[b]["snapshot"]["booking"]["flight_id"] for b in request["bookings"][:index]] + [chosen["flight_id"]]
                proposal = {"flights": [{**f, "capacity_for_proposal": f["total_seats"]["economy"]} for f in
                    ([flights[fid] for fid in dict.fromkeys(selected)] if efficient_reads else flights.values())],
                    "itineraries": [{**request, "flight_ids": selected}]}
                result = await call("run_skill_script", skill_id="plan-reaccommodation", inputs=proposal)
                if not result["proposal_feasible"]:
                    raise ValueError(result)
            await finish(plan)
            rebook = await begin("rebook-itinerary", [booking_id])
            if booking["flight_id"] != chosen["flight_id"]:
                booking = await call("change_booking", booking_id=booking_id, new_flight_id=chosen["flight_id"])
            if not booking["seat_number"]:
                seats = await call("get_seat_map", flight_id=chosen["flight_id"], seat_class="economy", status="available")
                assigned = await call("assign_seat", booking_id=booking_id, seat_number=seats["seats"][-1 if alternative else 0]["seat_number"])
                if efficient_reads:
                    booking = assigned
            await finish(rebook, [booking_id])
            settlement = await begin("settle-travel-account", [booking_id])
            if efficient_reads:
                from decimal import Decimal
                booking = dict(booking, balance_due=float(Decimal(str(booking["ticket_price"]))
                    + Decimal(str(booking["change_fee"])) - Decimal(str(booking["paid_amount"]))))
            else:
                booking = await call("get_booking", booking_id=booking_id)
            if booking["balance_due"]:
                await call("process_payment", booking_id=booking_id, amount=booking["balance_due"], method="credit_card")
            await finish(settlement)
            ground = await begin("reconcile-ground-services", [booking_id])
            for kind in ("baggage", "special_service"):
                for row in services[kind]:
                    if row["booking_revision"] != booking["revision"] or row["flight_id"] != booking["flight_id"]:
                        pk = "baggage_id" if kind == "baggage" else "service_id"
                        await call("transfer_" + kind, **{kind + "_id": row[pk], "booking_id": booking_id, "expected_revision": booking["revision"]})
            await finish(ground)
            handoff = await begin("complete-passenger-handoff", [booking_id])
            if booking["status"] != "checked_in":
                await call("check_in_passenger", booking_id=booking_id)
            args = {"booking_id": booking_id, "previous_clearance_id": clearances[request["bookings"][index - 1]]["clearance_id"] if index else ""}
            if trip:
                args["transfer_trip_id"] = trip["trip_id"]
            clearance = await call("issue_connection_clearance", **args)
            clearances[booking_id] = clearance
            await finish(handoff, [clearance["clearance_id"]])
        released = await begin("release-through-baggage")
        outputs = []
        for obligation in network["baggage_obligations"]:
            if obligation["origin_leg"] == index + 1:
                if efficient_reads and obligation["obligation_id"] in releases:
                    continue
                row = await call("release_through_baggage", obligation_id=obligation["obligation_id"], origin_clearance_id=clearances[obligation["origin_booking_id"]]["clearance_id"])
                releases[obligation["obligation_id"]] = row
                outputs.append(row["release_id"])
        await finish(released, outputs)
