"""Actor-supplied audience accounting controls actual synthetic response dynamics."""

from copy import deepcopy

from .runtime import SkillSocial
from .kernel import schema, require, instant
from .exposure_build import FORMAT


class ExposureSocial(SkillSocial):
    expected_format = FORMAT

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        require(self.public["format"] == self.world["format"] == self.expected_format, "EXPOSURE_NETWORK_FORMAT")
        # Historical task manifests keep their original script capabilities.
        self.script_files = dict(self.script_files)
        if any(s["name"] == "reconcile-audience-exposure" and
               "scripts/calculate_exposure.py" in s["files"] for s in self.public["skills"]):
            self.script_files["reconcile-audience-exposure"] = "scripts/calculate_exposure.py"
        text = {"type": "string", "minLength": 1}
        self.metadata.update({
            "list_audience_exposures": {"description": "Read actual immutable exposure-publication records in release order. No selected due set or corrected load is returned.",
                "input_schema": schema({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}}, [])},
            "bind_audience_exposure": {"description": "Check supplied positive historical publication identities and decayed segment loads at the current release time. Does not repair incorrect inputs.",
                "input_schema": schema({"post_id": text, "prior_publication_ids": {"type": "array", "items": text, "uniqueItems": True, "maxItems": 100},
                    "audience_loads": {"type": "object", "additionalProperties": {"type": "integer", "minimum": 0}}})}})

    def accounting(self, post):
        loads = {segment: 0 for segment in post["audience_segment_ids"]}
        windows = {r["segment_id"]: r["memory_hours"] * 3600 for r in self.public["exposure_policy"]["segments"]}
        selected = []
        for publication in self.state["publications"].values():
            elapsed = int((instant(self.now()) - instant(publication["published_at"])).total_seconds())
            positive = False
            for segment in set(loads) & set(publication["content"]["audience_segment_ids"]):
                contribution = publication["exposure"]["allocated_units"] * max(0, windows[segment] - elapsed) // windows[segment]
                loads[segment] += contribution
                positive |= contribution > 0
            if positive:
                selected.append(publication["publication_id"])
        return sorted(selected), loads

    def dispatch(self, name, args):
        if name == "list_audience_exposures":
            rows = [{"publication_id": p["publication_id"], "post_id": p["post_id"], "published_at": p["published_at"],
                "audience_segment_ids": p["content"]["audience_segment_ids"], "allocated_units": p["exposure"]["allocated_units"]} for p in self.state["publications"].values()]
            offset, limit = args.get("offset", 0), args.get("limit", 100)
            return {"records": deepcopy(rows[offset:offset + limit]), "total": len(rows)}
        if name == "bind_audience_exposure":
            post = self.owned("content_post", args["post_id"])
            require(post["status"] == "scheduled" and instant(post["scheduled_for"]) <= instant(self.now()), "EXPOSURE_BINDING_REQUIRES_DUE_POST")
            required, loads = self.accounting(post)
            require(sorted(args["prior_publication_ids"]) == required, "EXACT_POSITIVE_EXPOSURE_ORIGINS")
            require(args["audience_loads"] == loads, "ACTUAL_DECAYED_AUDIENCE_LOAD")
            binding = {**deepcopy(args), "as_of": self.now(), "post_version": post["version"]}
            self.state["exposure_bindings"][post["post_id"]] = binding
            return deepcopy(binding)
        return super().dispatch(name, args)

    def publish(self, post_id):
        post = self.owned("content_post", post_id)
        if post["status"] == "published":
            return super().publish(post_id)
        binding = self.state["exposure_bindings"].get(post_id)
        required, loads = self.accounting(post)
        require(binding and instant(binding["as_of"]) <= instant(self.now()) and binding["post_id"] == post_id
            and binding["post_version"] == post["version"]
            and sorted(binding["prior_publication_ids"]) == required and binding["audience_loads"] == loads, "CURRENT_EXPOSURE_BINDING_REQUIRED")
        brief = self.brief(post["brief_id"])
        variant = post["variant_index"]
        if post["role"] == "deployment":
            report = self.report(post["learning"]["report_ids"][0])
            variant = self.owned("content_post", report["winning_post_id"])["variant_index"]
        publication = super().publish(post_id)
        self.state["publications"][publication["publication_id"]]["exposure"] = {
            **deepcopy(binding), "as_of": self.now(), "allocated_units": brief["variant_exposure_units"][variant]}
        return deepcopy(self.state["publications"][publication["publication_id"]])

    def response_metrics(self, post_id):
        post = self.owned("content_post", post_id)
        exposure = self.state["publications"][post["publication_id"]]["exposure"]
        profile = deepcopy(self.state["response_profiles"][post_id])
        sensitivity = self.brief(post["brief_id"])["variant_fatigue_sensitivity"][post["variant_index"]]
        profile["clicks"] = profile["clicks"] * 100 // (100 + sum(exposure["audience_loads"].values()) * sensitivity)
        return profile
