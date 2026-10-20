"""Build nonadjacent cross-team interface-capacity obligations on real C25 documents."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from skillhorizon.construction_provenance import observe_call

from integrations.internal_mcp.io import read_json, write_json
from .build import build as base_build
from integrations.internal_mcp.topology import parent_positions, disclose_pattern


FORMAT = "skillhorizon.c25.capacity_network.v1"


def build(source, output, *, seed=25, width=3, depth=4, background=True, amendment=True, dependency_pattern="staggered", construction_profile="legacy"):
    parent_positions(0, 0, width, seed, dependency_pattern)
    public = observe_call(base_build, source, output, seed=seed, width=width, depth=depth, background=background, amendment=amendment, capacity_network=True, construction_profile=construction_profile)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    pages, blocks = world["tables"]["page"], world["tables"]["page_block"]
    requirements = {}
    for index, page in enumerate(pages):
        level, lane = divmod(index, width)
        parents = [pages[s * width + i]["page_id"] for s, i in parent_positions(level, lane, width, seed, dependency_pattern)]
        block = next(b for b in blocks if b["block_id"] == page["requirements_block_id"])
        required = json.loads(block["content"])
        required["parent_page_ids"] = list(dict.fromkeys(parents))
        required["local_limits"]["batch_size"] = 400 + lane * 100 + level * 30
        required["capacity_policy"] = {"page_id": page["page_id"], "stage": level + 1, "team": lane + 1,
            "local_capacity_units": 200 + lane * 40 if not level else 48 + seed % 5 * 8 + lane * 16,
            "consumers": [], "unit": "records_per_batch"}
        required["aggregation_policy"] = (
            "Union typed source fields and use the strictest retention limit. Batch capacity is the smaller of the local limit "
            "and local capacity plus the exact quota assigned to this page in each parent publication. A parent quota only "
            "activates at its declared consumer stage. Allocate each consumer min(max_units, floor(batch_size * weight / "
            "sum_consumer_weights)); preserve the unallocated remainder. Quotas are shared among all named consumers, "
            "including consumers several stages later; do not reallocate their reserved shares to immediate consumers.")
        requirements[page["page_id"]] = required
    for consumer in requirements.values():
        policy = consumer["capacity_policy"]
        for parent in consumer["parent_page_ids"]:
            requirements[parent]["capacity_policy"]["consumers"].append({"consumer_page_id": policy["page_id"],
                "activation_stage": policy["stage"], "weight": 1 + (policy["stage"] + policy["team"]) % 3, "max_units": 400})
    world["tables"]["relation"] = []
    for page in pages:
        required = requirements[page["page_id"]]
        next(b for b in blocks if b["block_id"] == page["requirements_block_id"])["content"] = json.dumps(required)
        item = next(i for i in world["tables"]["database_item"] if i["page_id"] == page["page_id"])
        item["relation_count"] = len(required["parent_page_ids"])
        for index, parent in enumerate(required["parent_page_ids"]):
            world["tables"]["relation"].append({"relation_id": f"{page['page_id']}-capacity-{index}", "task_id": public["task_id"],
                "workspace_id": public["workspace_id"], "source_page_id": page["page_id"], "target_page_id": parent,
                "relation_type": "depends_on", "item_id": item["item_id"], "health_status": "unchecked", "created_by": public["editor_user_id"],
                "created_at": world["clock_origin"], "updated_at": world["clock_origin"], "last_checked_at": None})
    for update in world["planning"]["scheduled_updates"]:
        revised = deepcopy(requirements[update["page_id"]])
        revised["local_fields"]["consent_epoch"] = {"type": "integer", "unit": "unix_seconds"}
        revised["local_limits"]["retention_days"] = 3
        revised["capacity_policy"]["local_capacity_units"] += 64
        update["requirements"] = revised
        update["reason"] = "A new capacity reservation adds 64 local records per batch; privacy review adds consent provenance and a three-day retention cap."
    public["format"] = world["format"] = FORMAT
    world["construction"]["dependency_pattern"] = dependency_pattern
    disclose_pattern(public, dependency_pattern, seed)
    public["goal"] += " Respect shared consumer capacity reservations across teams and nonadjacent stages. Publish actual quota values, not just dependency identities."
    public["policy"]["business_dependency_profile"] = "multi_origin_capacity_reservations"
    skill_path = output / "public/skills/reconcile-interface-contracts/SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\n\n## Shared capacity contracts\n\nWhen a document declares capacity_policy, do not use the legacy minimum-of-parent-batches rule. "
        "Read each parent's actual publication, locate the quota for this exact consumer and activation stage, and add those quotas to local capacity. "
        "Apply the local batch cap, distribute weighted shares among all future consumers (including nonadjacent stages), and retain the remainder. "
        "Never borrow a future consumer's reservation or substitute an equal-sized quota belonging to another team. The optional merge script computes only supplied data.\n", encoding="utf-8")
    for directory in ("checkpoint", "runtime"):
        write_json(output / directory / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": world["tables"], "scheduled_updates": world["planning"]["scheduled_updates"]})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=25)
    parser.add_argument("--width", type=int, default=3)
    parser.add_argument("--depth", type=int, default=4)
    parser.add_argument("--no-amendment", dest="amendment", action="store_false")
    print(build(**vars(parser.parse_args()))["task_id"])
