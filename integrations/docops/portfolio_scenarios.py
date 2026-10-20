"""Business scenario construction for rights-bearing document portfolios."""

from copy import deepcopy


FAMILIES = ("standard", "expiring_reservations", "independent_amendments", "expiring_amendments")


def configure(world, public, requirements, *, family, seed, width, depth):
    if family == "standard":
        return
    pages = world["tables"]["page"]
    mechanisms = []
    if family in ("expiring_reservations", "expiring_amendments"):
        for index, required in enumerate(requirements.values()):
            stage, lane = index // width + 1, index % width
            grant = required["portfolio_policy"]["local_grant"]
            grant["expires_stage"] = min(depth, stage + 1 + (lane + seed) % 2)
            own = required["portfolio_policy"]["purpose"]
            other = ("analytics", "serving", "compliance")[(stage + lane) % 3]
            grant["allowed_purposes"] = sorted({own, other})
        mechanisms = ["purpose_restricted_split", "expiring_nonadjacent_reservations", "origin_preserving_join"]
    if family in ("independent_amendments", "expiring_amendments"):
        for index, page in enumerate(pages):
            level, lane = divmod(index, width)
            parents = [] if not level else [pages[(level - 1) * width + lane]["page_id"]]
            if lane < 2 and level >= 2:
                parents.append(pages[(level - 1) * width + 1 - lane]["page_id"])
                if level >= 3:
                    parents.append(pages[1 - lane]["page_id"])
            requirements[page["page_id"]]["parent_page_ids"] = list(dict.fromkeys(parents))
        for required in requirements.values():
            required["capacity_policy"]["consumers"] = []
        for required in requirements.values():
            policy = required["capacity_policy"]
            for parent in required["parent_page_ids"]:
                requirements[parent]["capacity_policy"]["consumers"].append({"consumer_page_id": policy["page_id"],
                    "activation_stage": policy["stage"], "weight": 1 + (policy["stage"] + policy["team"]) % 3, "max_units": 400})
        relations = []
        for page in pages:
            required = requirements[page["page_id"]]
            item = next(r for r in world["tables"]["database_item"] if r["page_id"] == page["page_id"])
            item["relation_count"] = len(required["parent_page_ids"])
            for index, parent in enumerate(required["parent_page_ids"]):
                relations.append({"relation_id": f"{page['page_id']}-portfolio-{index}", "task_id": public["task_id"],
                    "workspace_id": public["workspace_id"], "source_page_id": page["page_id"], "target_page_id": parent,
                    "relation_type": "depends_on", "item_id": item["item_id"], "health_status": "unchecked", "created_by": public["editor_user_id"],
                    "created_at": world["clock_origin"], "updated_at": world["clock_origin"], "last_checked_at": None})
        world["tables"]["relation"] = relations
        mechanisms += ["overlapping_amendment_closures", "independent_track_reuse", "nonadjacent_origin_reservations"]
    if not mechanisms:
        raise ValueError("Unknown portfolio scenario")
    public["task_id"] += "-" + family.replace("_", "-")
    world["task_id"] = public["task_id"]
    for rows in world["tables"].values():
        for row in rows:
            row["task_id"] = public["task_id"]
    public["scenario"] = {"family": family, "seed": seed, "mechanisms": mechanisms,
        "world_size": {"width": width, "depth": depth}, "scope": "construction_development"}
    if family == "expiring_amendments":
        public["scenario"]["composition_of"] = ["expiring_reservations", "independent_amendments"]


def amendment_events(public, requirements):
    rows = list(requirements.values())
    width = public["scenario"]["world_size"]["width"]
    first, second = deepcopy(rows[0]), deepcopy(rows[width + 1])
    first["capacity_policy"]["local_capacity_units"] += 64
    first["local_fields"]["consent_epoch"] = {"type": "integer", "unit": "unix_seconds"}
    first["local_limits"]["retention_days"] = 3
    own = first["portfolio_policy"]["purpose"]
    revoked = next(p for p in first["portfolio_policy"]["local_grant"]["allowed_purposes"] if p != own)
    first["portfolio_policy"]["local_grant"]["allowed_purposes"].remove(revoked)
    second["portfolio_policy"]["local_grant"]["expires_stage"] = second["capacity_policy"]["stage"] + 1
    second["local_fields"]["usage_epoch"] = {"type": "integer", "unit": "unix_seconds"}
    second["local_limits"]["retention_days"] = 2
    reasons = (f"Origin owner revokes {revoked} use and requires consent provenance with a three-day cap.",
               "A different origin shortens its grant expiry and requires usage provenance with a two-day cap.")
    return [{"event_id": f"{public['task_id']}-amendment-{index}", "page_id": required["capacity_policy"]["page_id"],
             "requirements": required, "reason": reason} for index, (required, reason) in enumerate(zip((first, second), reasons), 1)]
