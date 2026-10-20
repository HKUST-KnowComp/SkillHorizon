---
name: schedule-cross-platform-release
description: Allocate shared account slots and publish approved posts only when due within their release windows.
---

# Schedule Cross-Platform Release

Inspect every campaign's pending release in the current wave. Accounts require at least one hour
between reserved or actual publication times. Finishing one entire campaign before considering the
others may miss their earlier variant windows. Separate variant publication from later deployments.

The optional allocate_slots.py script consumes `requests` (post_id, account_id, earliest, latest),
`occupied` (account_id, at), and `now`, returning a greedy feasible proposal or an error. It is not a
complete optimizer for arbitrary release-window patterns and does not reserve anything. Review and
submit each proposed time through schedule_post. Manual scheduling is also legal.

Advance the simulated clock deliberately and publish due reservations in time order. Advancing time
does not publish automatically. Cancellation frees a future reservation, not a historical publication.
An edit, revoked asset or stale approval requires repair before release. Preserve returned publications.

Outputs: reserved times and actual publication IDs/timestamps.
