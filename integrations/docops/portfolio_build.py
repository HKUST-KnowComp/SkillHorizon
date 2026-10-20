"""Build source-scoped, purpose-restricted quota portfolios on C25 documents."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
from skillhorizon.construction_provenance import construction_entry, observe_call
import shutil

from integrations.internal_mcp.io import read_json, write_json, local_entity_ids
from .capacity_build import build as capacity_build
from .portfolio_scenarios import FAMILIES, configure, amendment_events
from .kernel import KEYS
from integrations.internal_mcp.topology import PATTERNS, disclose_runtime_identity

FORMAT = "skillhorizon.c25.portfolio_network.v2"
PURPOSES = ["analytics", "serving", "compliance"]


@construction_entry
def build(source, output, *, seed=25, width=3, depth=4, background=True, amendment=None, scenario="standard", dependency_pattern="staggered", runtime_identity="legacy", construction_profile="legacy"):
    disclose_runtime_identity({}, runtime_identity)
    if scenario not in FAMILIES:
        raise ValueError("Unknown portfolio scenario")
    if scenario in ("independent_amendments", "expiring_amendments") and (width != 3 or depth < 4):
        raise ValueError("Independent amendments require three tracks and at least four stages")
    if scenario in ("independent_amendments", "expiring_amendments") and dependency_pattern != "staggered":
        raise ValueError("Independent amendment topology preserves a dedicated unaffected track")
    expected_amendment = scenario != "expiring_reservations"
    if amendment is None:
        amendment = expected_amendment
    if scenario != "standard" and amendment != expected_amendment:
        raise ValueError("Amendment setting conflicts with scenario")
    public = observe_call(capacity_build, source, output, seed=seed, width=width, depth=depth, background=background, amendment=amendment, dependency_pattern=dependency_pattern, construction_profile=construction_profile)
    disclose_runtime_identity(public, runtime_identity)
    output = Path(output)
    world = read_json(output / "checkpoint/world.json")
    requirements = {}
    for index, page in enumerate(world["tables"]["page"]):
        stage, lane = divmod(index, width)
        stage += 1
        block = next(b for b in world["tables"]["page_block"] if b["block_id"] == page["requirements_block_id"])
        required = json.loads(block["content"])
        purpose = PURPOSES[(stage + lane - 1) % len(PURPOSES)]
        allowed = list(PURPOSES) if stage == 1 or lane % 2 == 0 else sorted({purpose, PURPOSES[(stage + lane) % 3]})
        required["portfolio_policy"] = {"purpose": purpose, "local_grant": {"origin_page_id": page["page_id"],
            "origin_stage": stage, "allowed_purposes": allowed, "expires_stage": depth + 1 if stage == 1 else min(depth + 1, stage + 2)}}
        required["aggregation_policy"] = (
            "Preserve origin identity, allowed purposes and expiry through every split and join. Only the exact parent consumer-stage shares enter. "
            "Merge identical-origin shares only when their rights agree; every incoming source must authorize this page purpose and stage. "
            "Add the local grant, sort sources by expiry then origin ID, and retain up to the local batch cap; report withheld source quantities. "
            "For each retained source, allocate floor(source units * consumer weight / sum eligible consumer weights) among consumers whose purpose "
            "and activation stage satisfy that source, clipped by each consumer remaining max_units. Do not redistribute rounding or clipped leftovers. "
            "Preserve per-origin uncommitted shares. Union compatible fields and apply the strictest retention cap.")
        requirements[page["page_id"]] = required
    configure(world, public, requirements, family=scenario, seed=seed, width=width, depth=depth)
    if scenario == "standard":
        construction = world["construction"]
        positions = [(r["capacity_policy"]["stage"], r["capacity_policy"]["team"])
                     for r in requirements.values()]
        expected_positions = {(stage, team) for stage in range(1, depth + 1)
                              for team in range(1, width + 1)}
        if (any(construction.get(k) != v for k, v in
                {"seed": seed, "width": width, "depth": depth}.items())
                or len(positions) != width * depth or set(positions) != expected_positions
                or set(requirements) != {p["page_id"] for p in world["tables"]["page"]}):
            raise ValueError("Actual standard portfolio structure differs from construction arguments")
        public["scenario"] = {"family": "standard", "seed": seed,
            "world_size": {"width": width, "depth": depth}, "scope": "construction_development"}
    for required in requirements.values():
        for consumer in required["capacity_policy"]["consumers"]:
            consumer["purpose"] = requirements[consumer["consumer_page_id"]]["portfolio_policy"]["purpose"]
    for page in world["tables"]["page"]:
        block = next(b for b in world["tables"]["page_block"] if b["block_id"] == page["requirements_block_id"])
        block["content"] = json.dumps(requirements[page["page_id"]])
    for update in world["planning"]["scheduled_updates"]:
        revised = deepcopy(requirements[update["page_id"]])
        revised["local_fields"]["consent_epoch"] = {"type": "integer", "unit": "unix_seconds"}
        revised["local_limits"]["retention_days"] = 3
        revised["capacity_policy"]["local_capacity_units"] += 64
        grant = revised["portfolio_policy"]["local_grant"]
        revoked = next(p for p in PURPOSES if p != revised["portfolio_policy"]["purpose"])
        grant["allowed_purposes"] = [p for p in grant["allowed_purposes"] if p != revoked]
        update["requirements"] = revised
        update["reason"] = f"Origin owner revokes {revoked} use, adds 64 local units, and adopts consent provenance with a three-day retention cap."
    if scenario in ("independent_amendments", "expiring_amendments"):
        world["planning"]["scheduled_updates"] = amendment_events(public, requirements)
        public["planned_change_windows"] = 2
    public["format"] = world["format"] = FORMAT
    public["policy"]["business_dependency_profile"] = "origin_scoped_quota_portfolios"
    public["goal"] += " Preserve each original quota owner's purpose restrictions and expiry through all downstream reservations."
    skill = output / "public/skills/reconcile-interface-contracts"
    text = (skill / "SKILL.md").read_text()
    text = text.split("\n\n## Shared capacity contracts")[0]
    (skill / "SKILL.md").write_text(text + "\n\n## Origin-scoped quota portfolios\n\nRead the current requirements and actual parent publications. "
        "Use portfolio_policy and aggregation_policy, not the legacy scalar-capacity formula. Preserve original permissions and expiry at every split and join; "
        "equal-sized shares with different origins or rights are not interchangeable. Inspect source-level allocations, withheld and uncommitted amounts. "
        "The optional script computes only supplied public JSON and does not verify current publication state. If an origin owner revises its grant, "
        "re-read current publications and explicitly repair affected descendants; do not merely refresh their IDs.\n", encoding="utf-8")
    if scenario != "standard":
        path = skill / "SKILL.md"
        path.write_text(path.read_text() + "\n## Multiple release windows\n\nA source can remain valid for its own page while being expired or unauthorized for a later consumer. "
            "Do not borrow that consumer's allocation from another origin. After each published amendment, trace current parent relations before revising downstream contracts. "
            "A separate track with no dependency on the changed origin can retain its current publication and reviews. Continue until the declared change windows are drained.\n", encoding="utf-8")
    shutil.copyfile(Path(__file__).with_name("portfolio_rules.py"), skill / "scripts/merge_contracts.py")
    world, public = local_entity_ids(world, public, KEYS, json_text_fields=(("page_block", "content"),))
    public["policy"]["entity_identity"] = "short_task_local_ids_with_explicit_task_ownership"
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
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
    parser.add_argument("--no-amendment", dest="amendment", action="store_false", default=None)
    parser.add_argument("--scenario", choices=FAMILIES, default="standard")
    parser.add_argument("--dependency-pattern", choices=PATTERNS, default="staggered")
    parser.add_argument("--runtime-identity", choices=("legacy", "task_local_v1"), default="legacy")
    print(build(**vars(parser.parse_args()))["task_id"])
