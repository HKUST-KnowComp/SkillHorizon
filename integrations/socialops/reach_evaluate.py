"""Independent observed-conversion chronology and source-cohort reconstruction."""

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta

from .exposure_evaluate import account as exposure_account


def account(spec, world):
    expected, errors, metrics = exposure_account(spec, world)
    records, ages, overlap, joins = {}, Counter(), 0, []
    reactivated, mixed_expiry, cross_campaign, source_counts = 0, 0, 0, []
    try:
        state = world["social"]
        if state["cohort_response_profiles"] != spec["cohort_response_profiles"]:
            errors.append("FROZEN_COHORT_RESPONSE_CHANGED")
        policies = {p["segment_id"]: p for p in spec["public"]["reach_policy"]["segments"]}
        posts = {p["post_id"]: p for p in spec["initial_tables"]["content_post"]}
        observations = {}
        for row in world["tables"]["engagement_snapshot"]:
            if row["post_id"] in observations:
                errors.append("DUPLICATE_COHORT_OBSERVATION")
            observations[row["post_id"]] = row
        def date(value):
            result = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if result.tzinfo is None:
                raise ValueError("Timezone required")
            return result
        def event(kind, identifier):
            events = [e for e in state["events"] if e["event_type"] == kind and e["entity_id"] == identifier]
            if len(events) != 1:
                raise ValueError("Unique chronological event required")
            return events[0]
        for publication in state["publications"].values():
            post = posts[publication["post_id"]]
            released = event("post.published", post["post_id"])
            when = date(publication["published_at"])
            audiences = publication["content"]["audience_segment_ids"]
            covered = {s: set() for s in audiences}
            expired = {s: set() for s in audiences}
            contributing, origin_waves = [], set()
            cross_campaign_sources = set()
            for identifier, record in records.items():
                positive = False
                if record["sequence"] >= released["sequence"] or date(record["available_at"]) > when:
                    continue
                for segment in covered:
                    converted = record["converted_cohorts"].get(segment, [])
                    until = date(record["observed_until"]) + timedelta(hours=policies[segment]["conversion_cooldown_hours"])
                    if until > when and converted:
                        overlap += len(covered[segment].intersection(converted))
                        covered[segment].update(converted)
                        positive = True
                    elif converted:
                        expired[segment].update(converted)
                if positive:
                    contributing.append(identifier)
                    wave = spec["briefs"][post["brief_id"]]["wave"]
                    origin = spec["briefs"][posts[record["post_id"]]["brief_id"]]["wave"]
                    ages[wave - origin] += 1
                    origin_waves.add(origin)
                    if posts[record["post_id"]]["campaign_id"] != post["campaign_id"]:
                        cross_campaign_sources.add(identifier)
            eligible = {s: sorted(set(policies[s]["cohort_ids"]) - covered[s]) for s in covered}
            reactivated += sum(len(expired[s].intersection(eligible[s])) for s in eligible)
            mixed_expiry += int(any(expired.values()) and any(covered.values()))
            cross_campaign += bool(cross_campaign_sources)
            source_counts.append(len(contributing))
            ideal = {"post_id": post["post_id"], "post_version": publication["post_version"], "as_of": publication["published_at"],
                     "conversion_snapshot_ids": sorted(contributing), "eligible_cohorts": eligible}
            if publication["reach"] != ideal:
                errors.append("EXACT_OBSERVED_CONVERSION_REACH")
            joins.append(len(origin_waves))
            if post["role"] != "variant":
                continue
            profile = spec["cohort_response_profiles"][post["post_id"]]
            all_cohorts = [c for segment in audiences for c in policies[segment]["cohort_ids"]]
            available = [c for segment in eligible.values() for c in segment]
            total_weight = sum(profile["click_weights"][c] for c in all_cohorts)
            response = expected[post["post_id"]]
            response["impressions"] = response["impressions"] * len(available) // len(all_cohorts) if all_cohorts else 0
            response["clicks"] = response["clicks"] * sum(profile["click_weights"][c] for c in available) // total_weight if total_weight else 0
            if post["post_id"] in observations:
                observation = observations[post["post_id"]]
                measured = event("analytics.observed", observation["snapshot_id"])
                if measured["sequence"] <= released["sequence"]:
                    errors.append("CONVERSION_OBSERVATION_BEFORE_PUBLICATION")
                converted = {}
                for segment, group in eligible.items():
                    candidates = sorted(set(group).intersection(profile.get('conversion_partition', profile['conversion_order'])), key=profile["conversion_order"].index)
                    converted[segment] = candidates[:1] if response["clicks"] > 0 and profile["conversion_enabled"] else []
                records[observation["snapshot_id"]] = {"snapshot_id": observation["snapshot_id"], "post_id": post["post_id"],
                    "publication_id": publication["publication_id"], "observed_until": observation["observed_until"],
                    "available_at": observation["created_at"], "sequence": measured["sequence"], "converted_cohorts": converted}
        if state["conversion_records"] != records:
            errors.append("FROZEN_OBSERVED_CONVERSION_RECORDS")
        metrics["conversion_feedback"] = {"observed_conversion_snapshots": len(records), "positive_conversion_edges": sum(ages.values()),
            "conversion_wave_age_histogram": dict(sorted(ages.items())), "max_origin_waves_joined": max(joins, default=0),
            "deduplicated_overlap_contributions": overlap,
            "reactivated_cohort_contributions": reactivated, "mixed_active_expired_publications": mixed_expiry,
            "cross_campaign_conversion_publications": cross_campaign, "max_conversion_sources_joined": max(source_counts, default=0),
            "scope": "Synthetic observed cohort feedback, not human conversion rates or minimum horizon proof"}
    except (KeyError, TypeError, ValueError, IndexError, ZeroDivisionError):
        errors.append("MALFORMED_CONVERSION_REACH_EVIDENCE")
    return expected, errors, metrics
