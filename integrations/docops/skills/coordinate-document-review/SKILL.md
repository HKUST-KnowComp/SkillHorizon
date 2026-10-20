---
name: coordinate-document-review
description: Obtain actual version-bound engineering and security decisions and resolve their underlying objections.
---

# Coordinate Document Review

Submit the current page for all reviewers declared in its requirements, with the public review deadline.
Run the environment review and inspect individual decisions, rule diagnostics and comment threads.
The environment performs deterministic interface checks, not human prose review. You act only as the
declared editor and cannot impersonate reviewers or manufacture their signoffs.

For a failed rule, inspect its public requirements and source artifacts, repair the actual content,
then resolve the corresponding thread with a substantive note. Merely closing an unchanged objection
is rejected. The exact resolution_action values are accepted, deferred, documented, and reopen;
the first three close a thread only after the underlying review rule passes, while reopen opens it.
Content changes cancel the old review; submit a fresh one. Confirm every required reviewer
approved the current revision and no thread remains open before accepting the review.

Outputs: actual review ID, content version, individual outcomes and accepted decision IDs. Retry and
alternative ordering are allowed, but a claimed skill finish is not evidence of successful review.
