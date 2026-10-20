# Reconcile persistent audience exposure

Before publishing a due, scheduled post, inspect its current consented audience,
the public exposure policy and actual publication history, including older
campaigns. A newer campaign report does not replace that history. Published
exposure cannot be erased by editing later drafts.

## Fixed calculation script

Use `run_skill_script` with `skill_id="reconcile-audience-exposure"` to run
`scripts/calculate_exposure.py`. It performs exact integer arithmetic on supplied
JSON only; it cannot read environment state, discover missing records, verify
their authenticity or completeness, advance time, bind exposure or publish.
Calling the script is optional; the same exact business rules apply either way.

Supply `inputs` with:

- `post_id`: the target post ID (for attribution, not a state lookup).
- `now`: the current observed business clock, as an ISO timestamp with timezone.
- `audience_segment_ids`: the target post's current audience IDs.
- `segments`: the `read_task.exposure_policy.segments` rows, each with
  `segment_id` and positive integer `memory_hours`.
- `publications`: distinct actual publication rows, each with `publication_id`,
  `published_at`, `audience_segment_ids` and nonnegative integer `allocated_units`.
  These fields are available from `list_audience_exposures`; follow its pagination
  until all records are obtained. You may reuse already observed records and add
  actual `publish_post` receipts, projecting their `content.audience_segment_ids`
  and `exposure.allocated_units` into these flat fields. Do not infer actual
  deployment exposure from a draft's variant index.

You may omit cached records that public timestamps and windows prove fully
expired for every shared target segment, or whose audience has no intersection
with this post. Retain all potentially contributing records. This avoids
resending irrelevant history; it does not permit assuming missing pages are empty.

Example tool arguments (illustrative IDs only):

```json
{"skill_id":"reconcile-audience-exposure","inputs":{"post_id":"post-example","now":"2026-10-01T01:00:00Z","audience_segment_ids":["segment-example"],"segments":[{"segment_id":"segment-example","memory_hours":120}],"publications":[{"publication_id":"publication-example","published_at":"2026-10-01T00:00:00Z","audience_segment_ids":["segment-example"],"allocated_units":3}]}}
```

For each supplied source and shared target segment, compute
`floor(allocated_units * max(0, memory_seconds - elapsed_seconds) / memory_seconds)`.
Floor each source separately, then sum per segment. Include a publication ID
exactly when it contributes positively to at least one target segment. Preserve
zero loads for target segments with no positive contributions. The example
returns a load of 2 and includes `publication-example`.

The result contains `post_id`, `as_of`, `prior_publication_ids`, `audience_loads`
and `state_verified=false`. This flag is always false: an omitted record or wrong
clock can yield a valid calculation over incorrect inputs. The script result
is not a binding or proof of completion. Submit its IDs and loads separately via
`bind_audience_exposure(post_id, prior_publication_ids, audience_loads)`.

At release, a binding is reusable only while its post revision, exact sources
and loads still match current state. A clock advance alone does not invalidate
unchanged values. If the audience, relevant publication history or decayed loads
change, refresh the affected inputs, recalculate and rebind. Publication checks
current values independently. Compare actual observations before deploying a
winner; calculated exposure does not reveal future responses.
