---
name: rebook-itinerary
description: Change existing passenger reservations and select physical seats without duplicating capacity.
---

# Rebook itinerary

Recheck booking and target flight before committing a plan. Change the existing booking on the same
route, preserving its identity and payment ledger. Each change increments itinerary revision and may
add a fee; a failed operation commits nothing. Cabin capacity is reserved at booking/change time.
Assigning or changing a physical seat does not reserve a second place.

Every original booking ID listed in the task's requests must be completed. Cancelling one is
irreversible: no public operation restores that ID, and creating a replacement booking cannot
satisfy the original obligation. Use change_booking to preserve a live booking's identity.
A cheaper flight does not automatically refund historical payments or accumulated change fees.

Keep the returned revision and old-to-new itinerary association for financial and ground-service work.
Existing clearances may become stale after a change. Do not silently replace original baggage by
creating a new bag or assume that selecting a new flight automatically transfers services.

Useful tools: get_booking, change_booking, get_seat_map, assign_seat, change_seat.
