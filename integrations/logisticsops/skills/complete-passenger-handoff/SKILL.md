---
name: complete-passenger-handoff
description: Obtain current connected-travel clearances from settled bookings and valid predecessor evidence.
---

# Complete passenger handoff

Verify the seat, paid balance and original ground-service links before checking in. For a connection,
retrieve the preceding leg's actual clearance and verify passenger identity and current itinerary.
Issue the current leg's clearance with that predecessor, respecting minimum connection time and the
final arrival deadline. The first leg has no predecessor. Clearances are predeparture service records,
not evidence that an aircraft physically flew.

If an earlier booking or service changed, its old clearance and dependent clearances can be stale.
Reconcile the affected records and reissue in dependency order. Finishing a skill only records the
agent's declared work; it does not certify the whole travel task.

Useful tools: get_booking, list_booking_services, check_in_passenger, list_clearances, read_clearance,
issue_connection_clearance.
