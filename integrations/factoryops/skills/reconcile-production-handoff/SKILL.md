---
name: reconcile-production-handoff
description: Join terminal accepted outputs and actual dispositions with full material conservation.
---

# Reconcile the Production Handoff

Gather accepted, available output from every terminal work order. Check exact output-slot coverage and
quantity, not just a matching number of lots or completed runs. Retrieve the dispositions of the actual
producing attempts; the original failed attempt's disposition cannot substitute for the repaired one.

Verify that no queued/running attempt remains, declared updates were handled, and other material was
consumed through the BOM or explicit rework. Submit exact terminal unit IDs and producing disposition
IDs. Dispatch freezes those outputs so they cannot be consumed a second time.

Workflow completion markers do not establish manufacturing success. The independent benchmark grader
will check program/equipment lineage, timed runs, complete QC and material identity/conservation.
