"""Public-only reference policy with shared capacity and exact custody joins."""

from datetime import timedelta
from integrations.internal_mcp.reference_reads import ImmutableSkillDocuments

from .network_witness import run as run_network, instant, _protocol_caller


class FreightPolicy:
    def __init__(self, documents=None, skill_protocol=None):
        self.documents = documents if documents is not None else ImmutableSkillDocuments()
        self.skill_protocol = skill_protocol

    async def load(self, call, network):
        self.rows = {}
        self.unavailable = {s["service_id"] for s in network["freight_services"] if s.get("availability") == "unavailable"}
        for item in network["forwardings"]:
            row = await call("read_baggage_forwarding", forwarding_id=item["forwarding_id"])
            self.rows[row["forwarding_id"]] = row

    def invalid_dependents(self, invalid):
        return {r["forwarding_id"] for r in self.rows.values() if r["status"] != "scheduled"
                or r["service_id"] in self.unavailable
                or set(r["baggage_release_ids"] + r["prior_forwarding_ids"]) & invalid}

    async def cancel_invalid(self, call, invalid):
        for identifier, row in self.rows.items():
            if identifier in invalid and row["status"] == "scheduled":
                self.rows[identifier] = await call("cancel_baggage_forwarding", forwarding_id=identifier)

    def incoming(self, release, hub):
        rows = [r for r in self.rows.values() if r["status"] == "scheduled" and r["to_hub"] == hub
                and release["release_id"] in r["baggage_release_ids"]]
        if len(rows) != 1:
            raise ValueError("Reference policy has no unique actual incoming custody")
        return rows[0]

    async def advance(self, call, network, releases, clearances, flights, hub, *, alternative=False):
        origin = hub - 1
        if origin < 1:
            return
        active = [r for r in self.rows.values() if r["status"] == "scheduled"]
        moved = {i for r in active if r["from_hub"] == origin for i in r["baggage_release_ids"]}
        pending = [r for r in releases.values() if r["origin_leg"] <= origin and r["delivery_leg"] > hub and r["release_id"] not in moved]
        if not pending:
            return
        services = {s["service_id"]: s for s in network["freight_services"]}
        choices = sorted((s for s in services.values() if s["from_hub"] == origin and s["service_id"] not in self.unavailable), key=lambda s: (s["arrival_time"], s["service_id"]))
        assignments = {s["service_id"]: [] for s in choices}
        remaining = {s["service_id"]: s["capacity_kg"] - sum(r["weight_kg"] for r in active if r["service_id"] == s["service_id"]) for s in choices}
        pending.sort(key=lambda r: (r["delivery_leg"], r["passenger_id"], r["obligation_id"]), reverse=alternative)
        obligations = {o["obligation_id"]: o for o in network["baggage_obligations"]}
        def compatible(release, service):
            return ("handling_policy" not in network or
                    obligations[release["obligation_id"]]["required_handling"] in service["handling_capabilities"])
        if "handling_policy" in network:
            pending.sort(key=lambda r: sum(compatible(r, s) for s in choices))
        for release in pending:
            if release["origin_leg"] == origin:
                clearance = clearances[release["origin_booking_id"]]
                ready = instant(flights[clearance["snapshot"]["booking"]["flight_id"]]["scheduled_arrival"]) + timedelta(minutes=release["handling_minutes"])
                prior = None
            else:
                prior = self.incoming(release, origin)
                ready = instant(prior["arrival_time"]) + timedelta(minutes=services[prior["service_id"]]["handover_minutes"])
            feasible = [s for s in choices if compatible(release, s)
                        and remaining[s["service_id"]] >= release["bag"]["weight_kg"] and ready <= instant(s["departure_time"])]
            if not feasible:
                raise ValueError("No feasible public freight capacity/timetable")
            chosen = feasible[0]
            remaining[chosen["service_id"]] -= release["bag"]["weight_kg"]
            assignments[chosen["service_id"]].append((release, prior))
        await self.documents.read(call, "forward-through-baggage")
        run = (await call("begin_skill", skill_id="forward-through-baggage")
               if self.skill_protocol is None else None)
        outputs = []
        for service_id, rows in assignments.items():
            if not rows:
                continue
            for start in range(0, len(rows), 64):
                batch = rows[start:start + 64]
                result = await call("schedule_baggage_forwarding", service_id=service_id,
                    baggage_release_ids=[r["release_id"] for r, _ in batch],
                    prior_forwarding_ids=sorted({p["forwarding_id"] for _, p in batch if p}),
                    weight_kg=sum(r["bag"]["weight_kg"] for r, _ in batch))
                self.rows[result["forwarding_id"]] = result
                outputs.append(result["forwarding_id"])
        if run is not None:
            await call("finish_skill", run_id=run["run_id"], output_ids=outputs)

    def ground_bindings(self, cargo, hub, network):
        services = {s["service_id"]: s for s in network["freight_services"]}
        rows = {row["forwarding_id"]: row for release in cargo if release["origin_leg"] < hub
                for row in [self.incoming(release, hub)]}
        return sorted(rows), [instant(r["arrival_time"]) + timedelta(minutes=services[r["service_id"]]["handover_minutes"]) for r in rows.values()]


async def run(call, *, skill_protocol=None, max_calls=None, **options):
    call, counter = _protocol_caller(call, skill_protocol, max_calls)
    class OperationsChange(Exception):
        pass

    async def observe(name, **arguments):
        result = await call(name, **arguments)
        if isinstance(result, dict) and "operations_bulletin" in result:
            raise OperationsChange()
        return result

    if counter is not None:
        observe._freight_counter = counter

    # Re-read public state after an actual operations event, preserving completed work.
    documents = ImmutableSkillDocuments()
    for attempt in range(2):
        try:
            return await run_network(observe, freight=FreightPolicy(documents, skill_protocol),
                                     skill_protocol=skill_protocol, max_calls=max_calls, **options)
        except OperationsChange:
            if attempt:
                raise
