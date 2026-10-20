"""Standalone exposure-program entry; existing reach construction is untouched."""
from copy import deepcopy
from pathlib import Path

from skillhorizon.construction_provenance import construction_entry, observe_call
from integrations.internal_mcp.io import read_json, write_json, local_entity_ids
from integrations.internal_mcp.topology import disclose_runtime_identity
from .exposure_build import build as exposure_build, FORMAT
from .kernel import KEYS

MECHANISMS = ["exact_positive_exposure_origins", "decayed_shared_audience_load",
              "measured_variant_feedback", "cross_campaign_learning", "rights_withdrawal"]


@construction_entry
def build(source, output, *, seed=6, campaigns=2, waves=2, background=False,
          dependency_pattern="distributed", runtime_identity="task_local_v1"):
    disclose_runtime_identity({}, runtime_identity)
    public = observe_call(exposure_build, source, output, seed=seed, campaigns=campaigns,
                          waves=waves, background=background, dependency_pattern=dependency_pattern)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    if public["format"] != FORMAT or world["format"] != FORMAT or public["task_id"] != world["task_id"]:
        raise ValueError("Exposure delegate returned a different business program")
    if runtime_identity == "task_local_v1":
        world, public = local_entity_ids(world, public, KEYS)
    disclose_runtime_identity(public, runtime_identity)
    public["runtime_identity"] = runtime_identity
    public["dependency_construction"] = {"pattern": dependency_pattern, "revision": 1, "seed": seed,
        "scope": "construction_development_not_heldout", "minimum_horizon_proven": False}
    public["scenario"] = {"family": "persistent_exposure", "seed": seed,
        "world_size": {"campaigns": campaigns, "waves": waves}, "scope": "construction_development",
        "mechanisms": list(MECHANISMS)}
    world["construction"] = {"seed": seed, "campaigns": campaigns, "waves": waves,
                              "background_retained": background}
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    private = read_json(output / "private/initial_obligations.json")
    private.update(public=deepcopy(public), initial_tables=deepcopy(world["tables"]),
                   briefs=deepcopy(world["social"]["briefs"]),
                   response_profiles=deepcopy(world["social"]["response_profiles"]),
                   scheduled_updates=deepcopy(world["social"]["scheduled_updates"]))
    write_json(output / "private/initial_obligations.json", private)
    return public
