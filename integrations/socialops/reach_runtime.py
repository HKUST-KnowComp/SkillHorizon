"""Observed conversions alter shared future reach; no future-response lookup tools."""

from copy import deepcopy
from datetime import timedelta

from .exposure_runtime import ExposureSocial
from .kernel import require, schema, instant
from .reach_build import FORMAT


class ReachSocial(ExposureSocial):
    expected_format = FORMAT

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        text = {"type": "string", "minLength": 1}
        self.metadata.update({
            "list_audience_conversions": {"description": "Read only actual observed conversion snapshots, not future responses or the eligible audience answer.",
                "input_schema": schema({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [])},
            "bind_audience_reach": {"description": "Check supplied historical conversion sources and deduplicated reachable cohorts at current release time. Does not correct inputs.",
                "input_schema": schema({"post_id": text, "conversion_snapshot_ids": {"type": "array", "items": text, "uniqueItems": True, "maxItems": 100},
                    "eligible_cohorts": {"type": "object", "additionalProperties": {"type": "array", "items": text, "uniqueItems": True}}})}})

    def reach(self, post):
        policy = {p["segment_id"]: p for p in self.public["reach_policy"]["segments"]}
        suppressed = {s: set() for s in post["audience_segment_ids"]}
        required = []
        for record in self.state["conversion_records"].values():
            positive = False
            for segment in set(suppressed) & set(record["converted_cohorts"]):
                if instant(self.now()) < instant(record["observed_until"]) + timedelta(hours=policy[segment]["conversion_cooldown_hours"]):
                    suppressed[segment].update(record["converted_cohorts"][segment])
                    positive |= bool(record["converted_cohorts"][segment])
            if positive:
                required.append(record["snapshot_id"])
        eligible = {s: sorted(set(policy[s]["cohort_ids"]) - suppressed[s]) for s in suppressed}
        return sorted(required), eligible

    def dispatch(self, name, args):
        if name == "list_audience_conversions":
            records = list(self.state["conversion_records"].values())
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            return {"records": deepcopy(records[offset:offset + limit]), "total": len(records)}
        if name == "bind_audience_reach":
            post = self.owned("content_post", args["post_id"])
            require(post["status"] == "scheduled" and instant(post["scheduled_for"]) <= instant(self.now()), "REACH_BINDING_REQUIRES_DUE_POST")
            sources, eligible = self.reach(post)
            require(sorted(args["conversion_snapshot_ids"]) == sources, "EXACT_OBSERVED_CONVERSION_ORIGINS")
            require({s: sorted(c) for s, c in args["eligible_cohorts"].items()} == eligible, "ACTUAL_DEDUPLICATED_REACH")
            binding = {"post_id": post["post_id"], "conversion_snapshot_ids": sources, "eligible_cohorts": eligible,
                       "as_of": self.now(), "post_version": post["version"]}
            self.state["reach_bindings"][post["post_id"]] = binding
            return deepcopy(binding)
        return super().dispatch(name, args)

    def publish(self, post_id):
        post = self.owned("content_post", post_id)
        if post["status"] == "published":
            return super().publish(post_id)
        sources, eligible = self.reach(post)
        binding = self.state["reach_bindings"].get(post_id)
        require(binding and binding["post_id"] == post_id and binding["post_version"] == post["version"]
            and instant(binding["as_of"]) <= instant(self.now()) and binding["conversion_snapshot_ids"] == sources
            and binding["eligible_cohorts"] == eligible, "CURRENT_REACH_BINDING_REQUIRED")
        publication = super().publish(post_id)
        self.state["publications"][publication["publication_id"]]["reach"] = {**deepcopy(binding), "as_of": self.now()}
        return deepcopy(self.state["publications"][publication["publication_id"]])

    def response_metrics(self, post_id):
        result = super().response_metrics(post_id)
        post = self.owned("content_post", post_id)
        reach = self.state["publications"][post["publication_id"]]["reach"]["eligible_cohorts"]
        policy = {p["segment_id"]: p for p in self.public["reach_policy"]["segments"]}
        weights = self.state["cohort_response_profiles"][post_id]["click_weights"]
        total = sum(len(policy[s]["cohort_ids"]) for s in reach)
        possible_weight = sum(weights[c] for s in reach for c in policy[s]["cohort_ids"])
        result["impressions"] = result["impressions"] * sum(map(len, reach.values())) // total if total else 0
        result["clicks"] = result["clicks"] * sum(weights[c] for group in reach.values() for c in group) // possible_weight if possible_weight else 0
        return result

    def observe(self, post_id):
        observation = super().observe(post_id)
        identifier = observation["snapshot_id"]
        if identifier not in self.state["conversion_records"]:
            reach = self.state["publications"][observation["publication_id"]]["reach"]["eligible_cohorts"]
            profile = self.state["cohort_response_profiles"][post_id]
            converted = {s: [c for c in profile["conversion_order"] if c in group and c in profile.get('conversion_partition', profile['conversion_order'])][:1]
                         if profile["conversion_enabled"] and observation["clicks"] > 0 else [] for s, group in reach.items()}
            self.state["conversion_records"][identifier] = {"snapshot_id": identifier, "post_id": post_id,
                "publication_id": observation["publication_id"], "observed_until": observation["observed_until"],
                "available_at": observation["created_at"], "sequence": self.world["sequence"], "converted_cohorts": converted}
        return {**observation, "conversion": deepcopy(self.state["conversion_records"][identifier])}
