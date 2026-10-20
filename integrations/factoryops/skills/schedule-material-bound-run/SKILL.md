---
name: schedule-material-bound-run
description: Bind exact accepted material groups to a feasible equipment interval and release.
---

# Schedule a Material-bound Run

Read the current clock/reservations and available upstream inventory. Build one input group per output.
For root work this is one unit from its assigned raw lot. For assembly use the current order's BOM:
take the declared number of units from each named parent, which need not be one or an adjacent level.
Never reuse a unit across groups or orders. A rework group instead contains one quarantined
unit produced by this same work order; it preserves the original output slot.

Compute processing duration from the released cycle time and planned quantity. Optionally use
`scripts/find_interval.py` to find a free interval from supplied reservations. Input keys are `earliest`,
`duration_seconds`, `resources` with cell_id/tool_id/fixture_id, and `reservations` with scheduled_start,
scheduled_end and the same resource keys. Output is a candidate start/end, not a reservation.

Call the actual scheduling tool with work order, release, start time and chosen material groups. Check
the returned planned quantity, equipment and reserved material. Distinct programs still conflict when
they share equipment. If a queued release becomes stale, cancel that unstarted reservation, repair the
release and schedule again. Cancellation cannot undo an already started attempt's material consumption.
