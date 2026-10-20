---
name: inspect-production-order
description: Inspect actual work orders, equipment and upstream material obligations.
---

# Inspect a Production Order

Read the task and work order. Match logical manufacturing lane separately from physical cell: two
ready assembly orders can require the same bottleneck cell. Read current programs, equipment and
inventory. Root orders consume their assigned raw lot. Assembly outputs require the declared accepted
component count from each named upstream order in the current BOM, not arbitrary units with the same total.

Retain exact parent material identities, current status, program and equipment revisions, required
quantity, mandatory quality profile and any existing attempt. A reserved or consumed unit is not free
stock. On resume, inspect outstanding runs before creating a replacement. Poll the declared tooling
update after both root orders have enough accepted output. This simulator is not a live robot controller.
