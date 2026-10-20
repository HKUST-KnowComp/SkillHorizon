"""Public-tool-only reference execution. No evaluator or private task reads."""

from collections import defaultdict
from datetime import datetime
from itertools import product


def time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def allocate(public, flights, *, alternative=False):
    remaining = {key: row["available_seats"]["economy"] for key, row in flights.items()}
    assignments = {}
    for request in sorted(public["requests"], key=lambda r: (r["final_deadline"], r["passenger_id"]), reverse=False):
        feasible = []
        for identifiers in product(*(leg["alternative_flight_ids"] for leg in public["route"])):
            route = [flights[key] for key in identifiers]
            if any(remaining[key] < 1 for key in identifiers):
                continue
            if time(route[-1]["scheduled_arrival"]) > time(request["final_deadline"]):
                continue
            if any((time(b["scheduled_departure"]) - time(a["scheduled_arrival"])).total_seconds() < request["minimum_connection_minutes"] * 60 for a, b in zip(route, route[1:])):
                continue
            price = sum(row["base_price"]["economy"] for row in route)
            feasible.append((price, identifiers))
        if not feasible:
            raise ValueError("Reference allocation found no feasible public itinerary")
        feasible.sort()
        # Alternative portfolios are bounded to a small premium, preserving the
        # declared global spend headroom without consulting an evaluator.
        choice = feasible[0]
        if alternative:
            choice = next((row for row in feasible[1:] if row[0] <= feasible[0][0] + 10), choice)
        assignments[request["passenger_id"]] = choice[1]
        for identifier in choice[1]:
            remaining[identifier] -= 1
    return assignments


async def run(call, *, alternative=False, use_script=True, stop_after_legs=None, resume=False):
    public = await call("read_task")
    for skill in public["skills"]:
        await call("read_skill_file", skill_id=skill["name"])
    flights = {}
    for leg in public["route"]:
        for identifier in leg["alternative_flight_ids"]:
            flights[identifier] = await call("get_flight_status", flight_id=identifier)
    # Include already-held reservations in the complete portfolio proposal on resume.
    current_bookings = {}
    for request in public["requests"]:
        for booking_id in request["bookings"]:
            row = await call("get_booking", booking_id=booking_id)
            current_bookings[booking_id] = row
            if row["status"] in {"confirmed", "checked_in"} and row["flight_id"] in flights:
                flights[row["flight_id"]]["available_seats"]["economy"] += 1
    assignments = allocate(public, flights, alternative=alternative)
    clearances = {}
    if resume:
        offset = 0
        while True:
            page = await call("list_clearances", offset=offset, limit=30)
            for row in page["records"]:
                clearances[row["booking_id"]] = row["clearance_id"]
            offset += len(page["records"])
            if offset >= page["total"]:
                break
    cohorts = defaultdict(list)
    for request in public["requests"]:
        cohorts[request["cohort_id"]].append(request)
    cohort_order = sorted(cohorts, reverse=not alternative)

    async def start(skill, bookings):
        return (await call("begin_skill", skill_id=skill, booking_ids=bookings))["run_id"]

    async def finish(identifier, outputs=None):
        await call("finish_skill", run_id=identifier, output_ids=outputs or [])

    for leg in range(len(public["route"])):
        if stop_after_legs is not None and leg >= stop_after_legs:
            break
        for cohort in cohort_order:
            people = cohorts[cohort]
            booking_ids = [r["bookings"][leg] for r in people]
            if resume and all(identifier in clearances for identifier in booking_ids):
                continue
            run_id = await start("assess-disruption", booking_ids)
            rows, services = {}, {}
            for identifier in booking_ids:
                rows[identifier] = await call("get_booking", booking_id=identifier)
                services[identifier] = await call("list_booking_services", booking_id=identifier)
            await finish(run_id)
            run_id = await start("plan-reaccommodation", booking_ids)
            for request in people:
                await call("get_flight_status", flight_id=assignments[request["passenger_id"]][leg])
            if use_script and leg == 0:
                proposal = {"flights": [{**f, "capacity_for_proposal": f["total_seats"]["economy"]} for f in flights.values()],
                    "itineraries": [{**r, "flight_ids": assignments[r["passenger_id"]]} for r in public["requests"]]}
                result = await call("run_skill_script", skill_id="plan-reaccommodation", inputs=proposal)
                if not result["proposal_feasible"]:
                    raise ValueError(result)
            await finish(run_id)
            run_id = await start("rebook-itinerary", booking_ids)
            for request in people:
                identifier = request["bookings"][leg]
                flight_id = assignments[request["passenger_id"]][leg]
                if rows[identifier]["flight_id"] != flight_id:
                    rows[identifier] = await call("change_booking", booking_id=identifier, new_flight_id=flight_id)
                if not rows[identifier]["seat_number"]:
                    seats = await call("get_seat_map", flight_id=flight_id, seat_class="economy", status="available")
                    number = seats["seats"][-1 if alternative else 0]["seat_number"]
                    await call("assign_seat", booking_id=identifier, seat_number=number)
            await finish(run_id, booking_ids)
            run_id = await start("settle-travel-account", booking_ids)
            for identifier in booking_ids:
                row = await call("get_booking", booking_id=identifier)
                if row["balance_due"] > 0:
                    await call("process_payment", booking_id=identifier, amount=row["balance_due"], method="credit_card")
                rows[identifier] = row
            await finish(run_id)
            run_id = await start("reconcile-ground-services", booking_ids)
            for identifier in booking_ids:
                for kind in ("baggage", "special_service"):
                    for item in services[identifier][kind]:
                        if item["flight_id"] != rows[identifier]["flight_id"] or item["booking_revision"] != rows[identifier]["revision"]:
                            pk = "baggage_id" if kind == "baggage" else "service_id"
                            await call(f"transfer_{kind}", **{f"{kind}_id": item[pk], "booking_id": identifier,
                                       "expected_revision": rows[identifier]["revision"]})
            await finish(run_id)
            run_id = await start("complete-passenger-handoff", booking_ids)
            outputs = []
            for request in people:
                identifier = request["bookings"][leg]
                if rows[identifier]["status"] != "checked_in":
                    await call("check_in_passenger", booking_id=identifier)
                previous = clearances.get(request["bookings"][leg - 1], "") if leg else ""
                if previous:
                    await call("read_clearance", clearance_id=previous)
                receipt = await call("issue_connection_clearance", booking_id=identifier, previous_clearance_id=previous)
                clearances[identifier] = receipt["clearance_id"]
                outputs.append(receipt["clearance_id"])
            await finish(run_id, outputs)
    return {"clearances": clearances, "task_id": public["task_id"]}
