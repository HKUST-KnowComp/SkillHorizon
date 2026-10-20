"""Independent reconstruction from source exposure budgets and actual published content."""

from collections import Counter
from copy import deepcopy
from datetime import datetime


def account(spec, world):
    errors, metrics = [], {}
    expected = deepcopy(spec["response_profiles"])
    try:
        policy = spec["public"]["exposure_policy"]
        windows = {r["segment_id"]: 3600 * r["memory_hours"] for r in policy["segments"]}
        posts = {p["post_id"]: p for p in world["tables"]["content_post"]}
        publications = list(world["social"]["publications"].values())
        frozen = {p["post_id"]: p for p in spec["initial_tables"]["content_post"]}
        prior, graph, ages, fanout, origins = {}, {}, Counter(), Counter(), []
        def seconds(value):
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if date.tzinfo is None:
                raise ValueError("Timezone required")
            return int(date.timestamp())
        for publication in publications:
            post = posts[publication["post_id"]]
            brief = spec["briefs"][frozen[post["post_id"]]["brief_id"]]
            exposure = publication["exposure"]
            target = seconds(publication["published_at"])
            loads = {s: 0 for s in publication["content"]["audience_segment_ids"]}
            required, origin_waves = [], set()
            for identifier, earlier in prior.items():
                elapsed = target - earlier["time"]
                if elapsed < 0:
                    errors.append("EXPOSURE_TIME_ORDER")
                positive = False
                for segment in set(loads) & set(earlier["audiences"]):
                    remaining = max(0, windows[segment] - elapsed)
                    value, _ = divmod(earlier["units"] * remaining, windows[segment])
                    loads[segment] += value
                    positive |= value > 0
                if positive:
                    required.append(identifier)
                    ages[brief["wave"] - earlier["wave"]] += 1
                    origin_waves.add(earlier["wave"])
                    fanout[identifier] += 1
            if sorted(exposure["prior_publication_ids"]) != sorted(required):
                errors.append("EXACT_HISTORICAL_EXPOSURE_IDENTITIES")
            if exposure["audience_loads"] != loads:
                errors.append("SOURCE_RECONSTRUCTED_AUDIENCE_LOAD")
            if exposure["post_id"] != post["post_id"] or exposure["post_version"] != post["version"] or seconds(exposure["as_of"]) != target:
                errors.append("EXPOSURE_POST_VERSION_TIME")
            variant = frozen[post["post_id"]]["variant_index"]
            if post["role"] == "deployment":
                reports = [r for r in world["social"]["reports"].values() if r["brief_id"] == brief["brief_id"]]
                variant = frozen[reports[0]["winning_post_id"]]["variant_index"]
            units = brief["variant_exposure_units"][variant]
            if exposure["allocated_units"] != units:
                errors.append("FROZEN_EXPOSURE_BUDGET")
            if post["role"] == "variant":
                sensitivity = brief["variant_fatigue_sensitivity"][variant]
                expected[post["post_id"]]["clicks"] = (100 * spec["response_profiles"][post["post_id"]]["clicks"]) // (100 + sensitivity * sum(loads.values()))
            graph[publication["publication_id"]] = 1 + max((graph[p] for p in required), default=0)
            origins.append(len(origin_waves))
            prior[publication["publication_id"]] = {"time": target, "units": units, "audiences": list(loads), "wave": brief["wave"]}
        metrics = {"recorded_exposure_dependency_depth": max(graph.values(), default=0), "positive_exposure_edges": sum(ages.values()),
            "positive_exposure_wave_age_histogram": dict(sorted(ages.items())), "max_origin_waves_joined": max(origins, default=0),
            "max_actual_exposure_fanout": max(fanout.values(), default=0), "published_exposure_records": len(prior),
            "response_affected_posts": sum(expected[p] != spec["response_profiles"][p] for p in expected),
            "minimal_horizon_proven": False, "hard_audience_cap_enforced": False}
    except (KeyError, ValueError, TypeError, IndexError, ZeroDivisionError):
        errors.append("MALFORMED_EXPOSURE_EVIDENCE")
    return expected, errors, metrics
