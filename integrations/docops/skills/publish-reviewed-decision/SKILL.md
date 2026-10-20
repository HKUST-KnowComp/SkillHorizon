---
name: publish-reviewed-decision
description: Publish a current reviewed document and its accepted decisions as an immutable artifact.
---

# Publish Reviewed Decision

Read the page's current approval and exact accepted decision set. Confirm that no later edit or parent
change invalidated it and all discussion threads are resolved. Publish using this page's workspace,
target page and complete reviewed decision set. Publication does not approve proposals, close threads,
edit page blocks or synchronize tracker items.

Keep the returned publication ID, page ID and version together. Downstream consumers need the contract
and these bindings, not just a title or a previous skill's success message. Historical publications are
immutable and still readable after supersession; use the current one for new work.

Outputs: the actual publication artifact and source revision references.
