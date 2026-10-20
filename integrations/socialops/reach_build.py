"""Synthetic audience-cohort conversion feedback over persistent exposure."""

import argparse
from copy import deepcopy
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call

from integrations.internal_mcp.io import read_json, write_json, local_entity_ids
from .exposure_build import build as exposure_build
from .kernel import KEYS
from integrations.internal_mcp.topology import PATTERNS, disclose_runtime_identity

FORMAT = "skillhorizon.c06.reach_network.v2"
FAMILIES = ("standard", "overlapping_conversions", "cooldown_reactivation", "overlap_reactivation")


@construction_entry
def build(source, output, *, seed=6, campaigns=3, waves=4, background=True, scenario="standard", dependency_pattern="staggered", runtime_identity="legacy"):
    disclose_runtime_identity({}, runtime_identity)
    if scenario not in FAMILIES:
        raise ValueError("Unknown audience scenario")
    public = observe_call(exposure_build, source, output, seed=seed, campaigns=campaigns, waves=waves, background=background, dependency_pattern=dependency_pattern)
    disclose_runtime_identity(public, runtime_identity)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    segments = world["tables"]["audience_segment"]
    public["reach_policy"] = {"segments": [{"segment_id": s["segment_id"], "cohort_ids": [f"c{i:02d}" for i in range(16)],
        "conversion_cooldown_hours": 168 if index == 0 else 96} for index, s in enumerate(segments)],
        "rule": "At release time exclude the union of cohorts in actual already-observed conversion snapshots whose observed_until plus segment cooldown is later than now. "
                "Bind every snapshot with a positive intersecting cohort, even when another snapshot covers that same cohort. Do not sum overlaps. "
                "Zero reachable cohorts is a legal zero-reach publication, not permission to invent an audience. "
                "An earlier binding remains usable only if its post revision, exact contributing snapshots and eligible cohorts still match current state; publication rechecks them.",
        "measurement": "Response is synthetic and becomes visible only through actual observation. Eligible cohort composition and existing exposure fatigue affect impressions and clicks. "
                       "Each positive-click observation may convert at most one reached cohort per segment under its frozen synthetic response profile. "
                       "Anonymous cohorts are synthetic groups, not real people or inferred demographic traits."}
    profiles = {}
    for index, post in enumerate(world["tables"]["content_post"]):
        if post["role"] != "variant":
            continue
        shift = (seed + index * 3) % 16
        profiles[post["post_id"]] = {"conversion_enabled": True,
            "conversion_order": [f"c{(shift + j * 5) % 16:02d}" for j in range(16)],
            "click_weights": {f"c{i:02d}": 1 + ((i + seed + index) % 3) for i in range(16)}}
    if scenario in ("overlapping_conversions", "overlap_reactivation"):
        for post in world["tables"]["content_post"]:
            if post["role"] == "variant":
                wave = world["social"]["briefs"][post["brief_id"]]["wave"]
                shift = (seed + wave * 3) % 16
                profiles[post["post_id"]]["conversion_order"] = [f"c{(shift + j * 5) % 16:02d}" for j in range(16)]
        public["reach_policy"]["measurement"] += " Campaign outcomes may be correlated across overlapping audiences; observed cohort identities, not snapshot counts, determine reach."
    if scenario in ("cooldown_reactivation", "overlap_reactivation"):
        common = [s["segment_id"] for s in public["reach_policy"]["segments"][:2]]
        for brief in world["social"]["briefs"].values():
            brief["initial_variant_audiences"] = [sorted(set(group) | set(common)) for group in brief["initial_variant_audiences"]]
        public["reach_policy"]["segments"][0]["conversion_cooldown_hours"] = 120 + seed % 2 * 24
        public["reach_policy"]["segments"][1]["conversion_cooldown_hours"] = 18 + seed % 3 * 6
    if scenario == "standard":
        public["scenario"] = {"family": scenario, "seed": seed, "scope": "construction_development",
            "world_size": {"campaigns": campaigns, "waves": waves},
            "mechanisms": ["observed_conversion_evidence", "cohort_union_suppression"]}
    else:
        public["task_id"] += "-" + scenario.replace("_", "-")
        world["task_id"] = public["task_id"]
        for rows in world["tables"].values():
            for row in rows:
                row["task_id"] = public["task_id"]
        public["scenario"] = {"family": scenario, "seed": seed, "scope": "construction_development",
            "world_size": {"campaigns": campaigns, "waves": waves},
            "mechanisms": (["correlated_observed_conversions", "cross_campaign_union", "all_origin_evidence_join"]
                if scenario == "overlapping_conversions" else ["mixed_cooldown_expiry", "cohort_reactivation", "shared_segment_inheritance"])}
        if scenario == "overlap_reactivation":
            public["scenario"].update(composition_of=["overlapping_conversions", "cooldown_reactivation"],
                mechanisms=["correlated_observed_conversions", "cross_campaign_union", "all_origin_evidence_join",
                            "mixed_cooldown_expiry", "cohort_reactivation", "shared_segment_inheritance"])
    world["social"].update(reach_bindings={}, conversion_records={}, cohort_response_profiles=profiles)
    public["format"] = world["format"] = FORMAT
    public["goal"] += " Reconcile actual observed conversions across campaigns and deduplicate overlapping cohort suppression before release."
    public["skills"].append({"name": "reconcile-converted-audience", "files": ["SKILL.md"]})
    skill = output / "public/skills/reconcile-converted-audience/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# Reconcile converted audience\n\nRead the public cohort and cooldown policy, the current post audience, and actual conversion snapshots. "
        "Join snapshots to their real observed publication and time. Suppress the union of converted cohorts that remain in cooldown; duplicate conversions "
        "do not consume a cohort twice, but all contributing historical snapshot identities remain relevant. Bind the exact eligible cohorts and sources "
        "for the intended release. Binding requires a scheduled post whose release time is due. A newer report does not erase older conversion evidence. "
        "After a clock advance or new observation, an old binding is reusable only if the post revision, exact sources and cohort sets are unchanged. "
        "Publication revalidates them against current state; rebind when their actual meaning changes, not merely because the clock changed. "
        "Observe published variants only after their measurement window; do not predict hidden conversion outcomes. Published reach and observations are immutable.\n", encoding="utf-8")
    world, public = local_entity_ids(world, public, KEYS)
    public["policy"]["entity_identity"] = "short_task_local_ids_with_explicit_task_ownership"
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    spec = read_json(output / "private/initial_obligations.json")
    spec.update(public=deepcopy(public), cohort_response_profiles=deepcopy(world["social"]["cohort_response_profiles"]),
                initial_tables=deepcopy(world["tables"]), briefs=deepcopy(world["social"]["briefs"]),
                response_profiles=deepcopy(world["social"]["response_profiles"]), scheduled_updates=deepcopy(world["social"]["scheduled_updates"]))
    write_json(output / "private/initial_obligations.json", spec)
    config = read_json(output / "mcp.json")
    config["mcpServers"]["c06-social"]["args"][1] = "integrations.socialops.reach_server"
    write_json(output / "mcp.json", config)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=6)
    parser.add_argument("--campaigns", type=int, default=3)
    parser.add_argument("--waves", type=int, default=4)
    parser.add_argument("--scenario", choices=FAMILIES, default="standard")
    parser.add_argument("--dependency-pattern", choices=PATTERNS, default="staggered")
    parser.add_argument("--runtime-identity", choices=("legacy", "task_local_v1"), default="legacy")
    print(build(**vars(parser.parse_args()))["task_id"])
