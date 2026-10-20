---
name: assess-disruption
description: Establish passenger obligations, disrupted bookings and relevant airline state before recovery.
---

# Assess disruption

Read the travel request and identify the passenger or cohort in scope. Inspect each affected booking
and flight, retaining the booking revision, already-paid balance and previous itinerary. Read ground
services to distinguish original baggage/service identities from newly created or stale records.
Check the final arrival deadline and other passengers sharing alternatives. A cancelled flight does
not cancel its bookings or refund them automatically. Record observed facts and unresolved choices;
do not invent a downstream clearance or infer that a tool success completes the trip.

The original booking IDs in requests remain the required obligations. Cancelling a booking is
irreversible and also cancels its baggage and special services; no public operation restores it.
A newly created booking cannot substitute for that required original ID.

Useful tools: read_task, get_booking, get_flight_status, list_booking_services, read_clearance.
