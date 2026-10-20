---
name: plan-reaccommodation
description: Plan feasible connected itineraries under shared flight capacity, deadlines and fare exposure.
---

# Plan reaccommodation

Inspect candidate flights and their actual cabin availability, not just free physical seats. Compare
the whole itinerary: the next departure must leave enough time after the previous arrival. Consider
the final deadline and other cohorts before consuming a scarce early seat. Use existing payments
when estimating additional charges, and include voluntary change fees when revising a prior plan.
Alternative valid assignments are allowed; there is no prescribed flight portfolio.

The optional scripts/check_connections.py checks an explicitly proposed public itinerary against
times and capacity. Supply only public flight/request data. It neither reads environment files nor
produces a hidden reference solution. Its diagnostics do not reserve seats or authorize payments.

Call `run_skill_script` with `skill_id: "plan-reaccommodation"` and an `inputs` object containing
`flights` (at most 128) and `itineraries` (at most 64). Each flight supplies `flight_id`,
`departure_airport`, `arrival_airport`, `scheduled_departure`, `scheduled_arrival`, and the integer
`capacity_for_proposal`. Each itinerary supplies `passenger_id`, a nonempty `flight_ids` array,
`minimum_connection_minutes`, and `final_deadline`. Flight IDs must be unique in `flights` and
every itinerary must reference that supplied list. Use timezone-aware timestamps. The script
checks only this supplied proposal; a feasible empty proposal checks zero itineraries and does
not establish that any task obligation has been completed. Input errors identify the affected
public input path; correct the proposal before interpreting feasibility.

Useful tools: get_flight_status, get_booking, get_seat_map, run_skill_script.
