---
name: trace-planning-dependencies
description: Trace the requirements, document identities and current upstream publications for a PRD or RFC.
---

# Trace Planning Dependencies

Use this workflow when starting work on an unfamiliar document or investigating an upstream change.
Read the task and page detail. Identify the immutable requirements block, editable contract block,
linked tracker and declared parent pages. Inspect each parent's actual publication and accepted review;
an old artifact remains readable but its existence alone does not establish current approval.

Keep page identity, content version and publication identity together in notes or output references.
At a join, inspect every required parent, including one owned by another project. Follow dependency
relations to find affected consumers after an amendment. Do not assume all pages need restarting.

Outputs: the relevant page IDs, source publication IDs and unresolved questions. Skill completion is
only an observation; it does not approve a page or substitute for business state.
