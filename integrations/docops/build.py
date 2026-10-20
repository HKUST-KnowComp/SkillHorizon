"""Construct source-derived PRD/RFC networks with public contract obligations."""

import argparse
from copy import deepcopy
import json
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json
from .kernel import KEYS


FORMAT = "skillhorizon.c25.contracts.v1"
SKILLS = Path(__file__).with_name("skills")


def build(source, output, *, seed=25, width=2, depth=3, background=True, amendment=True, capacity_network=False, construction_profile="legacy"):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a fresh isolated output directory")
    if construction_profile not in {"legacy", "docops150_v1"}:
        raise ValueError("Unknown document construction profile")
    max_depth = 128 if construction_profile == "docops150_v1" else 8
    min_depth = 1 if construction_profile == "docops150_v1" else 2
    if type(seed) is not int or type(width) is not int or not 2 <= width <= 3 or type(depth) is not int or not min_depth <= depth <= max_depth:
        raise ValueError(f"Use integer seed, width 2..3, depth {min_depth}..{max_depth}")
    original = {kind: read_json(source / f"data/database/{kind}s.json") for kind in KEYS}
    rng = random.Random(seed)
    task_id = f"c25-{'capacity' if capacity_network else 'contract'}-{seed}-{width}w-{depth}d"
    tables = {kind: [] for kind in KEYS}
    stamp = "2026-10-08T00:00:00Z"

    def add(kind, template, identifier, values):
        row = {**deepcopy(template), **values, KEYS[kind]: identifier, "task_id": task_id}
        row.pop("id", None)
        tables[kind].append(row)
        return row

    workspace_id = task_id + "-workspace"
    workspace = add("workspace", rng.choice(original["workspace"]), workspace_id,
                    {"active_project_count": width, "archived_project_count": 0, "default_timezone": "UTC", "updated_at": stamp})
    users = []
    for role in ("editor", "engineering", "security"):
        user_id = task_id + "-" + role
        add("user", rng.choice(original["user"]), user_id, {"workspace_id": workspace_id, "status": "active",
            "review_role": role, "full_name": "Contract " + role.title(), "updated_at": stamp})
        users.append(user_id)
    editor, reviewers = users[0], users[1:]
    projects, views = [], []
    for lane in range(width):
        area_id, project_id, view_id = (f"{task_id}-{kind}-{lane + 1}" for kind in ("area", "project", "view"))
        add("product_area", rng.choice(original["product_area"]), area_id, {"workspace_id": workspace_id, "area_lead_user_id": editor, "active_project_count": 1})
        add("project", rng.choice(original["project"]), project_id, {"workspace_id": workspace_id, "area_id": area_id,
            "owner_user_id": editor, "status": "active", "page_count": depth, "decision_count": 0, "open_review_count": 0, "target_release": "2026-11-01"})
        add("database_view", rng.choice(original["database_view"]), view_id, {"workspace_id": workspace_id,
            "owner_user_id": editor, "database_kind": "rfc_tracker", "filters": {}, "is_default": True})
        projects.append((project_id, area_id))
        views.append(view_id)
    page_ids = [[f"{task_id}-level{level + 1}-lane{lane + 1}" for lane in range(width)] for level in range(depth)]
    requests = []
    for level in range(depth):
        for lane in range(width):
            page_id = page_ids[level][lane]
            project_id, area_id = projects[lane]
            parent_ids = [] if level == 0 else ([page_ids[level - 1][0], page_ids[level - 1][1]] if lane == width - 1 else [page_ids[level - 1][lane]])
            requirements_id, contract_id, narrative_id = (page_id + suffix for suffix in ("-requirements", "-contract", "-context"))
            local_fields = {f"lane{lane + 1}_step{level + 1}_{rng.choice(('request', 'event', 'record'))}": {
                "type": rng.choice(("string", "integer", "number")), "unit": rng.choice(("identifier", "count", "items"))}}
            local_limits = {"batch_size": rng.choice((128, 256, 512, 1024)), "retention_days": rng.choice((7, 14, 30, 90))}
            requirement = {"local_fields": local_fields, "local_limits": local_limits, "parent_page_ids": parent_ids,
                "reviewer_user_ids": reviewers, "aggregation_policy": "Union parent/local fields exactly; require matching definitions. "
                    "Use the largest mutually supported batch size and the strictest declared retention cap (minimum of source limits)."}
            page = add("page", rng.choice(original["page"]), page_id, {"workspace_id": workspace_id, "project_id": project_id,
                "area_id": area_id, "owner_user_id": editor, "last_edited_by": editor, "title": f"Interface contract L{level + 1} / team {lane + 1}",
                "page_type": "prd" if not level else "rfc", "lifecycle_status": "draft", "version": 1, "block_count": 3,
                "comment_thread_count": 0, "unresolved_comment_count": 0, "requirements_block_id": requirements_id,
                "contract_block_id": contract_id, "created_at": stamp, "updated_at": stamp})
            for position, (identifier, kind, content) in enumerate(((narrative_id, "paragraph", "Coordinate the interface update across dependent product teams. "
                    "Preserve the declared fields, limits, source revisions and review decisions."),
                    (requirements_id, "requirement", json.dumps(requirement)), (contract_id, "table", "{}")), 1):
                add("page_block", {}, identifier, {"page_id": page_id, "parent_block_id": None, "created_by": editor,
                    "block_type": kind, "position": position, "content": content, "checked": False, "created_at": stamp, "updated_at": stamp})
            item_id = page_id + "-tracker"
            add("database_item", rng.choice(original["database_item"]), item_id, {"page_id": page_id, "workspace_id": workspace_id,
                "project_id": project_id, "area_id": area_id, "view_id": views[lane], "owner_user_id": editor,
                "item_type": "prd_tracker" if not level else "rfc_tracker", "status": "drafting", "due_date": "2026-11-01",
                "title": page["title"] + " tracker", "linked_decision_ids": [], "relation_count": len(parent_ids), "properties": {}, "updated_at": stamp})
            for index, parent in enumerate(parent_ids):
                add("relation", {}, f"{page_id}-dependency-{index + 1}", {"workspace_id": workspace_id, "source_page_id": page_id,
                    "target_page_id": parent, "relation_type": "depends_on", "item_id": item_id, "health_status": "unchecked",
                    "created_by": editor, "created_at": stamp, "updated_at": stamp, "last_checked_at": None})
            requests.append({"page_id": page_id, "project_id": project_id, "tracker_item_id": item_id})
    skills = [{"name": p.name, "files": ["SKILL.md"] + (["scripts/merge_contracts.py"] if (p / "scripts").is_dir() else [])}
              for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "workspace_id": workspace_id, "editor_user_id": editor,
        "goal": "Reconcile, review and publish the interface contracts for every requested PRD/RFC, including downstream "
                "joins. Preserve declared source fields and limits, use current parent publications, obtain actual engineering "
                "and security signoffs, publish accepted decisions and synchronize each tracker to its own current publication. "
                "Requirements are in page blocks. Legal alternative ordering, prose and recovery are allowed. "
                "After the initial delivery, poll the planning change window and reconcile any announced amendments before final delivery.",
        "requests": requests, "review_due_at": "2026-11-01T00:00:00Z", "skills": skills,
        "planned_change_windows": int(amendment),
        "policy": {"review_mode": "deterministic_synthetic_contract_review_not_human_or_LLM_prose_review",
            "source_ownership": "existing_internal_assets_plus_new_active_documents", "skill_protocol": "observational_v1",
            "actor_cannot_impersonate_reviewers": True, "candidate_next_actions_exposed": False}}
    updates = []
    if amendment:
        root = tables["page"][0]
        block = next(b for b in tables["page_block"] if b["block_id"] == root["requirements_block_id"])
        revised = json.loads(block["content"])
        revised["local_fields"]["consent_epoch"] = {"type": "integer", "unit": "unix_seconds"}
        revised["local_limits"]["retention_days"] = 3
        updates.append({"event_id": task_id + "-privacy-amendment", "page_id": root["page_id"],
            "requirements": revised, "reason": "Privacy review requires consent provenance and a three-day source retention cap."})
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "sequence": 0, "clock_origin": stamp,
        "planning": {"next_id": 1, "publications": {}, "revision_history": {}, "skill_runs": {}, "active_skill_run": None,
            "scheduled_updates": updates, "applied_updates": []},
        "construction": {"seed": seed, "width": width, "depth": depth}}
    write_json(output / "background.json", original if background else {kind: [] for kind in KEYS})
    write_json(output / "public/task.json", public)
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "scheduled_updates": updates})
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c25-planning": {"command": sys.executable,
        "args": ["-m", "integrations.docops.server", str(output), str(output / "runtime")], "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=25)
    parser.add_argument("--width", type=int, default=2)
    parser.add_argument("--depth", type=int, default=3)
    args = parser.parse_args()
    public = build(args.source, args.output, seed=args.seed, width=args.width, depth=args.depth)
    print({"task_id": public["task_id"], "pages": len(public["requests"]), "provider_calls": 0, "qualified": False})


if __name__ == "__main__":
    main()
