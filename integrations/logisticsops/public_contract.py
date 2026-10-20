"""Public rule descriptions shared by new tasks and tool metadata."""

VERSION = "logisticsops_public_contract_v3"

GROUND_TIMING = (
    "All durations are in minutes. duration_minutes = base_travel_minutes + "
    "ceil(weight_kg / weight_step_kg) * minutes_per_weight_step + "
    "wheelchair_count * wheelchair_minutes; ceil rounds upward to an integer. "
    "departure_time must be at or after every inbound flight's scheduled_arrival, "
    "each due bag's origin flight scheduled_arrival plus handling_minutes, and the "
    "previous scheduled trip of the same vehicle's arrival_time plus turnaround_minutes, if any. "
    "For forwarded bags, also wait for actual freight arrival and handover. "
    "arrival_time must equal departure_time plus duration_minutes exactly; a later arrival is not accepted. "
    "Use timezone-aware timestamps. Each onward flight must leave at or after arrival_time "
    "plus the group's boarding_buffer_minutes."
)

BOOKING_IDENTITY = (
    "Every original booking ID listed in requests must be completed; a newly created booking "
    "cannot replace it. cancel_booking is irreversible: there is no public operation to "
    "restore a cancelled booking ID, and its baggage and special services are cancelled too. "
    "A cancelled flight does not itself cancel the booking."
)

REFUND_ENTITLEMENT = (
    "At cancellation, the refund entitlement in cents is floor(gross completed charge cents * percent / 100). "
    "The percent is 100 if the flight is cancelled; otherwise it is 80 for a confirmed booking "
    "or 50 for a checked_in booking. Flight-cancelled status takes precedence. Already completed "
    "refunds reduce the remaining entitlement; each refund is also limited by the selected payment "
    "method's completed charges minus completed refunds."
)

PAYMENT_RECOVERY = (
    "Changing to a cheaper flight does not automatically refund historical payments or accumulated fees. "
    "process_refund requires a cancelled booking and is limited by its recorded entitlement "
    "and original payment-method balance. Cancelling a required original booking to obtain a "
    "refund makes that original obligation impossible to complete. Tool-attempt allowance "
    "does not increase the task-wide net-spend limit. " + REFUND_ENTITLEMENT
)
