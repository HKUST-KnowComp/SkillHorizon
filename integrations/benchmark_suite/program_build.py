"""Finalize an explicit existing business program without altering its rules."""
from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json, local_entity_ids
from integrations.internal_mcp.topology import disclose_runtime_identity


def finalize(output, public, *, program, family, seed, sizes, background,
             runtime_identity, keys, private_state=None, json_text_fields=(), seed_key="seed",
             dependency_pattern=None):
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    if public["format"] != program or world["format"] != program or public["task_id"] != world["task_id"]:
        raise ValueError("Delegated business program differs")
    if runtime_identity == "task_local_v1":
        world, public = local_entity_ids(world, public, keys, json_text_fields=json_text_fields)
    disclose_runtime_identity(public, runtime_identity)
    public["scenario"] = {"family": family, seed_key: seed, "world_size": sizes,
                          "scope": "construction_development"}
    world["construction"] = {**world.get("construction", {}), "seed": seed, **sizes, "background_retained": background}
    if dependency_pattern is not None:
        public["dependency_construction"] = {"pattern": dependency_pattern, "revision": 1, "seed": seed,
            "scope": "construction_development_not_heldout", "minimum_horizon_proven": False}
        world["construction"]["dependency_pattern"] = dependency_pattern
    private = {"public": deepcopy(public), "initial_tables": deepcopy(world["tables"])}
    if program == "skillhorizon.c09.transfer_network.v1":
        private["initial_ids"] = {kind: [row[keys[kind]] for row in world["tables"][kind]]
                                  for kind in ("booking", "baggage", "special_service", "payment")}
    elif program == "skillhorizon.c47.workload_network.v1":
        private.update({name: deepcopy(world["finops"][name])
                        for name in ("resources", "usage", "scheduled_updates")})
    elif program == "skillhorizon.c25.capacity_network.v1":
        private["scheduled_updates"] = deepcopy(world["planning"]["scheduled_updates"])
    elif program == "skillhorizon.c06.measured_waves.v1":
        private.update({name: deepcopy(world["social"][name])
                        for name in ("briefs", "response_profiles", "scheduled_updates")})
    elif private_state is not None:
        private["initial_state"] = deepcopy(world[private_state])
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", private)
    return public
