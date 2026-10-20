"""DocOps long business portfolios; fresh instances, unchanged review semantics."""

from copy import deepcopy
import json
from pathlib import Path
import random

from integrations.internal_mcp.io import read_json, write_json
from skillhorizon.construction_provenance import construction_entry
from .declaration_build import build as declaration_build, DECLARATION_GUIDANCE
from .kernel import KEYS

REVISION = "docops_portfolio150_v2"
FAMILIES = ("expiring_reservations", "independent_amendments", "expiring_amendments")
PATTERNS = ("chain_join", "braided", "distributed")


@construction_entry
def build(source, output, *, seed, depth, scenario, topology="distributed", background=True, documents=None):
    if scenario not in FAMILIES or topology not in PATTERNS:
        raise ValueError("Unknown DocOps construction condition")
    if depth < 2:
        raise ValueError("DocOps v2 requires at least two stages and six documents")
    output = Path(output)
    if documents is not None and (type(documents) is not int or not 6 <= documents <= 3 * depth
                                  or documents <= 3 * (depth - 1)):
        raise ValueError("documents must be at least six and cover every complete stage plus part or all of the final stage")
    public = declaration_build(source, output, seed=seed, width=3, depth=depth,
        scenario="standard", amendment=False, dependency_pattern="staggered", background=background,
        runtime_identity="task_local_v1", construction_profile="docops150_v1")
    world = read_json(output / "checkpoint/world.json")
    if documents is not None:
        selected = {p["page_id"] for p in world["tables"]["page"][:documents]}
        for kind in ("page", "page_block", "database_item"):
            world["tables"][kind] = [r for r in world["tables"][kind] if r["page_id"] in selected]
        public["requests"] = [r for r in public["requests"] if r["page_id"] in selected]
        for project in world["tables"]["project"]:
            project["page_count"] = sum(p["project_id"] == project["project_id"] for p in world["tables"]["page"])
    pages, blocks = world["tables"]["page"], world["tables"]["page_block"]
    by_block = {b["block_id"]: b for b in blocks}
    topology_rng = random.Random(f"docops-topology-v1:{seed}:{topology}")
    facts = random.Random(f"docops-business-v1:{seed}")
    identity = random.Random(f"docops-identities-v1:{seed}")
    unaffected = topology_rng.randrange(3)
    affected_lanes = [n for n in range(3) if n != unaffected]
    requirements = {}
    for index, page in enumerate(pages):
        level, lane = divmod(index, 3)
        stage = level + 1
        req = json.loads(by_block[page["requirements_block_id"]]["content"])
        parents = []
        if level:
            parents.append(pages[(level - 1) * 3 + lane]["page_id"])
            lanes = list(range(3)) if scenario == "expiring_reservations" else (
                [unaffected] if lane == unaffected else affected_lanes)
            others = [n for n in lanes if n != lane]
            if others and (topology != "chain_join" or level % 3 == 1):
                other = topology_rng.choice(others)
                parents.append(pages[(level - 1) * 3 + other]["page_id"])
            if level >= 3 and topology == "distributed":
                origin = topology_rng.randrange(max(0, level - 5), level - 1)
                parents.append(pages[origin * 3 + topology_rng.choice(lanes)]["page_id"])
        req["parent_page_ids"] = list(dict.fromkeys(parents))
        req["portfolio_representation"] = "unordered_collections_v2"
        req["aggregation_policy"] += (
            " Report collections (allocations, portfolio, sources, withheld/uncommitted sources and allowed purposes) "
            "are unordered; duplicates and altered quantities/rights are invalid. The expiry/origin ordering governs "
            "the calculation of retained quantities, not the serialization order of the resulting report.")
        purpose = facts.choice(("analytics", "serving", "compliance"))
        others = [p for p in ("analytics", "serving", "compliance") if p != purpose]
        permissions = sorted([purpose] + facts.sample(others, facts.choice((1, 2))))
        expires = depth + 1 if scenario == "independent_amendments" else min(depth + 1, stage + facts.randint(1, 5))
        req["portfolio_policy"] = {"purpose": purpose, "local_grant": {
            "origin_page_id": page["page_id"], "origin_stage": stage,
            "allowed_purposes": permissions, "expires_stage": expires}}
        req["local_limits"] = {"batch_size": facts.randint(220, 580),
            "retention_days": facts.choice((3, 7, 14, 30, 60))}
        req["capacity_policy"].update(local_capacity_units=facts.randint(70, 240), consumers=[])
        # Interface fields are actual required schema additions, not length padding.
        req["local_fields"] = {f"field_{identity.getrandbits(40):010x}": definition
                              for definition in req["local_fields"].values()}
        requirements[page["page_id"]] = req
    for pid, req in requirements.items():
        for parent in req["parent_page_ids"]:
            requirements[parent]["capacity_policy"]["consumers"].append({
                "consumer_page_id": pid, "activation_stage": req["capacity_policy"]["stage"],
                "purpose": req["portfolio_policy"]["purpose"], "weight": facts.randint(1, 7),
                "max_units": facts.randint(60, 350)})
    for req in requirements.values():
        facts.shuffle(req["capacity_policy"]["consumers"])
    if scenario in {"expiring_reservations", "expiring_amendments"}:
        # Expiry is a real business condition even in the shortest portfolios.
        # A root's own quota is positive and usable locally but expires before
        # an actual later consumer; other pages still introduce local grants.
        roots = [r for r in requirements.values() if not r["parent_page_ids"] and r["capacity_policy"]["consumers"]]
        expiring = facts.choice(roots)
        grant = expiring["portfolio_policy"]["local_grant"]
        consumer = facts.choice(expiring["capacity_policy"]["consumers"])
        grant["allowed_purposes"] = sorted(set(grant["allowed_purposes"]) | {consumer["purpose"]})
        grant["expires_stage"] = expiring["capacity_policy"]["stage"]
    world["tables"]["relation"] = []
    items = {r["page_id"]: r for r in world["tables"]["database_item"]}
    for page in pages:
        pid = page["page_id"]
        req = requirements[pid]
        by_block[page["requirements_block_id"]]["content"] = json.dumps(req)
        items[pid]["relation_count"] = len(req["parent_page_ids"])
        for parent in req["parent_page_ids"]:
            world["tables"]["relation"].append({"relation_id": f"relation-{identity.getrandbits(48):012x}",
                "task_id": public["task_id"], "workspace_id": public["workspace_id"],
                "source_page_id": pid, "target_page_id": parent, "relation_type": "depends_on",
                "item_id": items[pid]["item_id"], "health_status": "unchecked",
                "created_by": public["editor_user_id"], "created_at": world["clock_origin"],
                "updated_at": world["clock_origin"], "last_checked_at": None})
    updates = []
    if scenario != "expiring_reservations":
        # Real revision windows open only after a complete delivered portfolio.
        # Mid-graph owners change; the independent track and ancestors survive.
        for n, lane in enumerate(affected_lanes):
            desired_level = max(0, depth // 2 - 1) + n
            candidates = [(i // 3, p) for i, p in enumerate(pages) if i % 3 == lane
                          and requirements[p["page_id"]]["capacity_policy"]["consumers"]]
            _, selected_page = min(candidates, key=lambda pair: abs(pair[0] - desired_level))
            pid = selected_page["page_id"]
            revised = deepcopy(requirements[pid])
            grant = revised["portfolio_policy"]["local_grant"]
            if n == 0:
                candidates = [p for p in grant["allowed_purposes"] if p != revised["portfolio_policy"]["purpose"]]
                revoked = facts.choice(candidates)
                grant["allowed_purposes"].remove(revoked)
                revised["capacity_policy"]["local_capacity_units"] += facts.randint(30, 90)
                reason = f"Owner revokes {revoked} use, changes its quota, and requires consent provenance."
            else:
                grant["expires_stage"] = revised["capacity_policy"]["stage"]
                reason = "Owner restricts its grant to the current release stage and requires usage provenance."
            revised["local_fields"][f"amendment_{identity.getrandbits(40):010x}"] = {"type": "integer", "unit": "unix_seconds"}
            revised["local_limits"]["retention_days"] = 1
            updates.append({"event_id": f"amendment-{identity.getrandbits(48):012x}",
                "page_id": pid, "requirements": revised, "reason": reason})
    world["planning"]["scheduled_updates"] = updates
    public["planned_change_windows"] = len(updates)
    public["scenario"] = {"family": scenario, "world_size": {"width": 3, "depth": depth, "documents": len(pages)},
        "dependency_pattern": topology, "instance_revision": REVISION}
    public["policy"]["instance_revision"] = REVISION
    public["policy"].update(portfolio_representation="unordered_collections_v2", response_profile="document_receipts_v2")
    public["dependency_construction"] = {"pattern": topology, "interpretation": "public parent IDs define the actual graph"}
    for request in public["requests"]:
        req = requirements[request["page_id"]]
        request.update(stage=req["capacity_policy"]["stage"], parent_page_ids=req["parent_page_ids"])
    public["request_directory_policy"] = "The request directory gives stable dependency identities and stages, not document contents or current completion. Read requirements on demand. Change bulletins identify every invalidated page; other publications remain current."
    public["skill_declaration_guidance"] = DECLARATION_GUIDANCE.strip()
    for skill in public["skills"]:
        path = output / "public/skills" / skill["name"] / "SKILL.md"
        path.write_text(path.read_text().replace(DECLARATION_GUIDANCE, "\nController declaration rules are in read_task.skill_declaration_guidance.\n"))
    # Identity permutation is independent of rights, capacities and topology.
    remap = {}
    for kind, key in KEYS.items():
        for row in world["tables"][kind]:
            remap[row[key]] = f"{kind.replace('_', '-')}-{identity.getrandbits(48):012x}"
    def replace(value):
        if isinstance(value, str):
            return remap.get(value, value)
        if isinstance(value, list):
            return [replace(v) for v in value]
        if isinstance(value, dict):
            return {remap.get(k, k): replace(v) for k, v in value.items()}
        return value
    for block in blocks:
        try:
            block["content"] = json.dumps(replace(json.loads(block["content"])))
        except (ValueError, TypeError):
            pass
    world, public = replace(world), replace(public)
    previous_id = public["task_id"]
    task_id = f"{previous_id}-docops-v2-{scenario}-{topology}-{len(pages)}p"
    def retag(value):
        if isinstance(value, list):
            for item in value:
                retag(item)
        elif isinstance(value, dict):
            for key, item in value.items():
                if key == "task_id" and item == previous_id:
                    value[key] = task_id
                else:
                    retag(item)
    retag(world)
    retag(public)
    identity.shuffle(public["requests"])
    world["construction"].update(profile=REVISION, topology=topology)
    for folder in ("checkpoint", "runtime"):
        write_json(output / folder / "world.json", world)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public,
        "initial_tables": world["tables"], "scheduled_updates": world["planning"]["scheduled_updates"]})
    return public
