---
name: calibrate-and-validate-program
description: Obtain environment measurements and validate exact program/equipment revisions.
---

# Calibrate and Validate a Program

Inspect the actual tool calibration state. Measure a due tool using the environment calibration test.
Read the returned bias and compute its opposite as compensation. Apply that value with the matching
measurement ID and tool revision. An email, date or valid flag cannot replace a calibration result.

Validate the prepared program. Inspect all returned checks and the bound program/tool/profile/zone
snapshot. A failed check needs an actual repair followed by a new validation. Publish using the passing
current validation. Keep the release ID, calibration and program identities for scheduling.

Tool drift invalidates future use of releases tied to the previous revision; it does not change completed
executions. Recalibrate and prepare a new program revision for a previously released program. Do not
relabel its old validation or silently reuse a queued run's stale release.
