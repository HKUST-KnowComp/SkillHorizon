---
name: settle-travel-account
description: Reconcile fares, prior charges and cancellation refunds for a passenger itinerary.
---

# Settle travel account

Cancellation refund entitlement is computed in integer USD cents as
`floor(gross completed charge cents * percent / 100)`: 100% if the flight is cancelled;
otherwise 80% for a confirmed booking or 50% for a checked-in booking. Flight cancellation
takes precedence. Previous refunds reduce the remaining entitlement, and refunds through
one payment method cannot exceed that method's completed charges minus refunds. This does
not make cancellation of a required original booking recoverable.

Read the actual payment ledger and current fare plus accumulated fees. Charge only the outstanding
USD cent amount using an admitted payment method. Reusing a prior payment means retaining its ledger
ownership, not charging the full new fare again. Consider the task-wide net-spend limit across cohorts.

For cancelled bookings, refunds are limited by the recorded cancellation entitlement and the remaining
balance of the original payment method. Do not infer a full refund from the existence of a payment.
Record actual charge/refund outputs; a proposed amount or script calculation is not a payment receipt.

Refunds require a cancelled booking. Cancellation cannot be undone: the original booking ID and
its baggage/special services cannot be restored by any public operation. A new booking does not
replace an original ID required by the task. Do not treat cancellation/refund as a reversible way
to reduce spending while keeping that required booking complete. Rebooking to a cheaper flight
does not automatically refund historical charges or accumulated fees. Extra tool attempts do not
increase the task-wide net-spend allowance.

Useful tools: get_booking, process_payment, cancel_booking, process_refund.
