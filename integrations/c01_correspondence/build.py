"""Build isolated project correspondence without inventing historical attachment bytes."""

import argparse
from collections import Counter
from copy import deepcopy
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json


KEYS = {"account": "account_id", "attachment": "attachment_id", "contact": "contact_id", "email": "email_id",
    "folder": "folder_id", "label": "label_id", "rule": "rule_id", "template": "template_id"}
FORMAT = "skillhorizon.c01.correspondence.v1"
ORIGIN = "2026-10-01T08:00:00Z"
SKILLS = Path(__file__).with_name("skills")


def build(source, output, *, seed=1, cases=2, phases=2, background=True, funding_network=False, line_items_per_brief=4):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use fresh isolated output")
    maximum_phases = 6 if funding_network else 4
    minimum_cases = 1 if funding_network else 2
    if type(seed) is not int or type(cases) is not int or not minimum_cases <= cases <= 4 or type(phases) is not int or not 2 <= phases <= maximum_phases:
        raise ValueError(f"Use integer seed, {minimum_cases}..4 cases and 2..{maximum_phases} phases")
    if type(line_items_per_brief) is not int or not 4<=line_items_per_brief<=8 or (line_items_per_brief!=4 and not funding_network):
        raise ValueError("Funding supports4..8 workstreams; standard correspondence requires4")
    original = {kind: read_json(source / f"data/database/{kind}s.json") for kind in KEYS}
    rng = random.Random(seed)
    folder_owner = {r["folder_id"]: r["account_id"] for r in original["folder"]}
    counts = Counter(folder_owner.get(r["folder_id"]) for r in original["email"])
    actor = sorted(counts, key=lambda k: (-counts[k], str(k)))[0]
    task_id = f"c01-{'funding' if funding_network else 'mail'}-{seed}-{cases}c-{phases}p"
    if line_items_per_brief!=4:
        task_id+=f"-{line_items_per_brief}i"
    tables = {kind: [] for kind in KEYS}
    inherited = deepcopy(original) if background else {kind: [] for kind in KEYS}

    def add(kind, identifier, values):
        row = {**deepcopy(values), KEYS[kind]: identifier, "task_id": task_id}
        tables[kind].append(row)
        return row

    original_actor = next(a for a in original["account"] if a["account_id"] == actor)
    add("account", actor, {**original_actor, "status": "active", "role": "project-coordinator", "storage_used_mb": 0,
        "storage_limit_mb": 128, "storage_used_bytes": 0, "storage_limit_bytes": 128 * 1024 * 1024,
        "auto_reply_enabled": False, "auto_reply_message": "", "updated_at": ORIGIN})
    actor_folders = [r for r in original["folder"] if r["account_id"] == actor]
    for row in actor_folders:
        add("folder", row["folder_id"], {**row, "email_count": 0, "unread_count": 0})
    inherited["account"] = [r for r in inherited["account"] if r["account_id"] != actor]
    inherited["folder"] = [r for r in inherited["folder"] if r["account_id"] != actor]
    accounts = {}
    case_ids = [f"case-{task_id}-{i}" for i in range(cases)]
    for case_id in case_ids:
        accounts[case_id] = {}
        for role in ("owner", "finance", "operations"):
            identifier = f"account-{case_id}-{role}"
            accounts[case_id][role] = identifier
    auditor = f"account-{task_id}-audit"
    outsider = f"account-{task_id}-outside-consultant"
    roles = {identifier: role for group in accounts.values() for role, identifier in group.items()}
    roles.update({auditor: "audit", outsider: "unassigned-consultant"})
    for identifier, role in roles.items():
        add("account", identifier, {"email_address": identifier + "@synthetic.invalid", "display_name": identifier.removeprefix("account-"),
            "role": role, "department": "Synthetic projects", "status": "active", "signature": "", "auto_reply_enabled": False,
            "auto_reply_message": "", "storage_used_mb": 0, "storage_limit_mb": 128, "storage_used_bytes": 0,
            "storage_limit_bytes": 128 * 1024 * 1024, "created_at": ORIGIN, "updated_at": ORIGIN})
        for kind in ("inbox", "sent", "drafts", "archive"):
            add("folder", f"folder-{identifier}-{kind}", {"account_id": identifier, "name": kind.title(), "type": kind,
                "parent_folder_id": None, "email_count": 0, "unread_count": 0, "created_at": ORIGIN, "updated_at": ORIGIN})
        add("contact", f"contact-{identifier}", {"owner_account_id": actor, "contact_name": identifier.removeprefix("account-"),
            "display_name": identifier.removeprefix("account-"), "contact_email": identifier + "@synthetic.invalid", "account_id": identifier,
            "company": "Synthetic projects", "phone": "", "group_name": role, "is_favorite": False, "notes": role,
            "created_at": ORIGIN, "updated_at": ORIGIN})
    template_id = f"template-{task_id}-review"
    add("template", template_id, {"name": "Project phase review request", "subject_pattern": "$case_id phase $phase: $role review",
        "body_pattern": "$payload_json", "category": "request", "placeholders": ["case_id", "phase", "role", "payload_json"],
        "usage_count": 0, "created_by": actor, "status": "active", "created_at": ORIGIN, "updated_at": ORIGIN})
    staff = [actor, auditor] + [v for group in accounts.values() for v in group.values()]
    nodes, documents = {}, {}
    for phase in range(phases):
        for index, case_id in enumerate(case_ids):
            node_id = f"{case_id}-phase-{phase}"
            parents = [] if phase == 0 else [f"{c}-phase-{phase-1}" for c in (case_id, case_ids[(index - 1) % cases])]
            if cases==1:
                parents=list(dict.fromkeys(parents))
            document_id = f"document-{node_id}-brief"
            items = [{"item_id": f"item-{node_id}-{j}", "description": f"Project {index} phase {phase} workstream {j}",
                "quantity": rng.randint(1, 5), "unit_cost_cents": rng.randint(10, 150) * 100, "work_days": rng.randint(1, 4)} for j in range(line_items_per_brief)]
            content = {"kind": "phase_brief", "node_id": node_id, "case_id": case_id, "phase": phase, "revision": 1,
                "line_items": items, "parent_node_ids": parents, "handling": "Internal project staff only; do not deliver to unassigned consultants."}
            documents[document_id] = {"document_id": document_id, "owner_account_id": accounts[case_id]["owner"], "filename": node_id + "-brief.json",
                "current_revision": 1, "allowed_recipient_ids": staff, "visible_to_actor": True, "authorship": "environment",
                "versions": {"1": {"revision": 1, "content": content, "created_at": ORIGIN}}}
            nodes[node_id] = {"node_id": node_id, "case_id": case_id, "phase": phase, "parent_node_ids": parents,
                "brief_document_id": document_id, "reviewer_ids": {k: accounts[case_id][k] for k in ("finance", "operations")},
                "owner_account_id": accounts[case_id]["owner"], "audit_account_id": auditor, "deadline": "2026-10-02T08:00:00Z"}
    amended = next(iter(nodes))
    update = {"node_id": amended, "document_id": nodes[amended]["brief_document_id"], "after_review_roles": ["finance", "operations"],
        "new_unit_cost_cents": documents[nodes[amended]["brief_document_id"]]["versions"]["1"]["content"]["line_items"][0]["unit_cost_cents"] + 1700,
        "new_work_days": documents[nodes[amended]["brief_document_id"]]["versions"]["1"]["content"]["line_items"][0]["work_days"] + 2}
    skills = [{"name": p.name, "files": [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in {".md", ".py"}]}
        for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "actor": actor, "origin": ORIGIN, "case_ids": case_ids, "nodes": list(nodes.values()),
        "skills": skills, "template_id": template_id, "project_staff_ids": staff, "directory_account_ids": [actor] + list(roles),
        "goal": "Complete all project phase correspondence. Obtain finance and operations replies for current briefs and acknowledged parent dispatches, "
            "assemble the actual budget/start/finish package, and send to each case owner with audit in BCC before the deadline. Preserve confidential attachments "
            "and past deliveries. A declared amendment follows the first pair of reviews for " + amended + ".",
        "policy": {"skill_protocol": "observational_v1", "simulation": "bounded_deterministic_correspondents_not_human_negotiation",
            "reply_delay_seconds": 60, "maximum_clock_seconds": 86400, "maximum_sent_messages": 512,
            "declared_amendment_node_id": amended, "historical_attachment_content": "metadata_only_unavailable",
            "privacy_scope": "actual_attachment_delivery_not_general_freeform_DLP", "source_archive_visible_messages": counts[actor] if background else 0}}
    public["policy"].update(finance_formula="sum(quantity * unit_cost_cents) across current brief line_items",
        operations_formula="sum(work_days) across line_items; each work_days is for the whole line, not each quantity unit",
        parent_schedule_formula="start_day = maximum acknowledged parent finish_day, or zero for a root; finish_day = start_day + duration_days",
        audit_privacy="Exposing the audit account in a dispatch To or CC header is irreversible, even after a corrected dispatch",
        template_support="Exact declared dollar-template placeholders; historical templates with unsupported syntax remain read-only examples")
    state = {"next_id": 1, "nodes": nodes, "documents": documents, "messages": {}, "events": [], "pending": [],
        "processed_deliveries": [], "reviews": {}, "acknowledgements": {}, "amendment": update, "applied_amendment": None,
        "skill_runs": {}, "active_skill_run": None}
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "mail": state, "sequence": 0, "clock": ORIGIN}
    # Legacy archive bytes are explicitly metadata-based; new messages use frozen payload bytes.
    attachments = {r["attachment_id"]: r for r in inherited["attachment"]}
    for account in tables["account"]:
        owned_folders = {r["folder_id"] for r in tables["folder"] if r["account_id"] == account["account_id"]}
        emails = [r for r in inherited["email"] if r.get("folder_id") in owned_folders]
        size = sum(len((r.get("subject", "") + r.get("body", "")).encode()) + sum(max(0, int(attachments.get(i, {}).get("size_bytes", 0))) for i in r.get("attachment_ids", [])) for r in emails)
        account.update(storage_used_bytes=size, storage_used_mb=size / 1048576)
        for folder in tables["folder"]:
            if folder["account_id"] == account["account_id"]:
                rows = [r for r in emails if r["folder_id"] == folder["folder_id"]]
                folder.update(email_count=len(rows), unread_count=sum(not r.get("is_read", False) for r in rows))
    write_json(output / "background.json", inherited)
    write_json(output / "public/task.json", public)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "initial_state": state})
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c01-correspondence": {"command": sys.executable,
        "args": ["-m", "integrations.c01_correspondence.server", str(output), str(output / "runtime")], "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--cases", type=int, default=2)
    parser.add_argument("--phases", type=int, default=2)
    args = parser.parse_args()
    print(build(args.source, args.output, seed=args.seed, cases=args.cases, phases=args.phases)["task_id"])
