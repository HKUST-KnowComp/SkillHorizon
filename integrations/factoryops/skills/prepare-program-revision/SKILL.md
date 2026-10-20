---
name: prepare-program-revision
description: Prepare actual versioned process content without changing released history.
---

# Prepare a Program Revision

Inspect program detail and ordered waypoints. The work order, physical cell, tool and mandatory profile
must match the intended operation. Process offset affects actual synthetic measurements; a structurally
valid program can still produce out-of-tolerance units. Cycle time sets the real simulated duration.

Edit an unreleased program's parameters or waypoints as needed. Every content change invalidates prior
validation. For a previously released program, create a separate draft revision; do not rewrite the old
release or a completed run. Retain new program/waypoint identities and the actual source revision.

Check waypoint sequence, speed limits, coordinate envelope and payload. These are declared discrete
constraints, not collision or dynamics proof. A fresh release is required after changed calibration,
even if the visible process parameters are unchanged.
