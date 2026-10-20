---
name: analyze-campaign-experiment
description: Collect actual environment observations and select the correct normalized campaign winner.
---

# Analyze Campaign Experiment

After all declared variants are published, launch their exact brief comparison. Wait until the fixed public
observation interval has elapsed and collect each variant's observation. The environment owns metrics;
neither the coordinator nor this skill can invent clicks or impressions. Repeated collection is idempotent.

Compare clicks/impressions exactly. A variant can have fewer clicks but a better rate. Zero impressions
have rate zero; tied rates use the lexicographically smallest post ID. Bind each observation to its
actual publication, content revision and exposure interval, then submit the proposed winner and exact
snapshot set. Preserve the returned report for deployment and future campaign-wave joins.

The optional compare_rates.py consumes `items` with `id`, `clicks`, and `impressions`; it returns scores
and winning_id. Use post IDs for variant ranking or report IDs for parent-report ranking. It computes
from supplied data only and cannot verify that the supplied observations are current or authentic.

Outputs: actual observation IDs, selected post and completed report ID.
