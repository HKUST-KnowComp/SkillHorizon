---
name: revise-planning-document
description: Update a PRD or RFC's actual editable content and record a proposed decision without bypassing review.
---

# Revise Planning Document

Inspect the page before editing. Apply the reconciled JSON to its contract block using the declared
editor identity. Preserve requirements and unrelated narrative content. A content edit increments the
page version and invalidates reviews of this page and dependent consumers. A no-op edit does not.

Record a proposed decision with rationale, alternatives and the correct linked tracker. When replacing
an accepted decision, reference it through `supersedes_decision_id`; it remains accepted until the new
proposal is actually reviewed and accepted. Do not declare a proposed decision accepted yourself.

Outputs: edited page/version and proposed decision ID. Re-read the page if several workflows have
interleaved edits; an earlier version assumption may no longer hold.
