"""Persistent shared-audience exposure with cross-wave measured feedback."""

import argparse
from copy import deepcopy
from pathlib import Path
from skillhorizon.construction_provenance import observe_call

from integrations.internal_mcp.io import materialize_skills, read_json, write_json
from .build import build as base_build
from integrations.internal_mcp.topology import parent_positions, disclose_pattern


FORMAT = "skillhorizon.c06.exposure_network.v1"


def build(source, output, *, seed=6, campaigns=3, waves=4, background=True, dependency_pattern="staggered"):
    parent_positions(0, 0, campaigns, seed, dependency_pattern)
    output = Path(output)
    public = observe_call(base_build, source, output, seed=seed, campaigns=campaigns, waves=waves, background=background, exposure_network=True)
    world = read_json(output / "checkpoint/world.json")
    briefs = list(world["social"]["briefs"].values())
    for index, brief in enumerate(briefs):
        wave, lane = divmod(index, campaigns)
        if wave:
            parents = [briefs[s * campaigns + i]["brief_id"] for s, i in parent_positions(wave, lane, campaigns, seed, dependency_pattern)]
            brief["parent_brief_ids"] = list(dict.fromkeys(parents))
        brief["variant_exposure_units"] = [3 + lane, 5 + lane]
        brief["variant_fatigue_sensitivity"] = [1, 5]
        for variant, post_id in enumerate(brief["variant_post_ids"]):
            profile = world["social"]["response_profiles"][post_id]
            profile.update(impressions=2000, clicks=(110 if variant == 0 else 150) + profile["clicks"] % 13)
    segments = briefs[0]["allowed_audience_ids"]
    policy = {"segments": [{"segment_id": segment, "memory_hours": 168 if index == 0 else (72 if index % 2 else 120)} for index, segment in enumerate(segments)],
        "load_rule": "For each current audience segment, sum floor(prior_exposure_units * remaining_seconds / memory_seconds) over actual prior publications targeting that segment. Ignore zero contributions. At release, an existing binding may be reused only if its post revision, exact sources and loads still match current state. Otherwise rebind; publication independently checks the current values.",
        "response_rule": "Impressions use the fixed synthetic source response; clicks are floor(source_clicks * 100 / (100 + total_segment_load * variant_fatigue_sensitivity)). Future base responses remain unobserved until the measurement window ends.",
        "constraint_scope": "Shared exposure affects response, not a hard ban or demographic inference. Deployment exposure uses the actual winning variant's declared units."}
    public["format"] = world["format"] = FORMAT
    disclose_pattern(public, dependency_pattern, seed)
    public["exposure_policy"] = policy
    public["goal"] += " Reconcile persistent audience exposure across campaigns before each release; use actual measured responses under that exposure, not assumed fixed winners."
    public["skills"].append({"name": "reconcile-audience-exposure",
        "files": ["SKILL.md", "scripts/calculate_exposure.py"]})
    public["policy"]["exposure_calculation"] = "explicit_input_skill_script_v1"
    materialize_skills(Path(__file__).with_name("exposure_skills") / "reconcile-audience-exposure",
        output / "public/skills/reconcile-audience-exposure")
    world["social"]["exposure_bindings"] = {}
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"],
        "briefs": world["social"]["briefs"], "response_profiles": world["social"]["response_profiles"], "scheduled_updates": world["social"]["scheduled_updates"]})
    config = read_json(output / "mcp.json")
    config["mcpServers"]["c06-social"]["args"][1] = "integrations.socialops.exposure_server"
    write_json(output / "mcp.json", config)
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=6)
    parser.add_argument("--campaigns", type=int, default=3)
    parser.add_argument("--waves", type=int, default=4)
    print(build(**vars(parser.parse_args()))["task_id"])
