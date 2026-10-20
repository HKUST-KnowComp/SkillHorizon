---
name: execute-and-observe-batch
description: Execute timed material-consuming attempts and collect actual per-unit observations.
---

# Execute and Observe a Batch

Inspect the scheduled attempt and its start time. Start at the reserved time after checking the current
release and declared tooling update. Starting consumes the reserved component units and snapshots the
actual program/calibration evidence. Multiple cells can run concurrently, but a cell retains its fixture
until the batch is dispositioned; do not assume a delayed operator has already unloaded it.

Advance the simulation clock to the processing end, accounting for other queued start times. Then
collect the actual environment-generated observations. Every produced unit needs every mandatory
metric, including its run, output slot, parent material, profile and release identity.

Do not invent measured values, claim a cycle count, or finalize immediately after starting. A read or
clock advance alone does not manufacture measurements. Retain the real output lot/unit/check IDs for
disposition. The response model is synthetic, not sensor telemetry from real machinery.
