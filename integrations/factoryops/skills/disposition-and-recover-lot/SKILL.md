---
name: disposition-and-recover-lot
description: Classify actual unit-level quality and explicitly rework quarantined output.
---

# Disposition and Recover a Lot

Read every actual QC observation for the run. Match each expected unit and metric to the same run,
profile and release. Apply inclusive minimum/maximum bounds. A defective unit is counted once even if
both its metrics fail. Missing or duplicate checks are not evidence of a pass.

Optionally use `scripts/aggregate_quality.py` with supplied `run_id`, `release_id`, `profile_id`,
`unit_ids`, `metric_names` and `checks`. It returns accepted and quarantined unit IDs after verifying
coverage and identity. The script does not create checks or complete the run.

Submit the actor-computed partition through finalize_program_run. Read the actual disposition and
produced quantity. Quarantined units cannot feed the next assembly. Diagnose the program/calibration,
prepare a corrected release, and schedule an explicit rework attempt using those quarantined units.
Retain the original failure and the new attempt's parent identities. Rework is bounded and may fail;
it is not a reset, an automatic repair, or a way to fabricate extra raw material.
