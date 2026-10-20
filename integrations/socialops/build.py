"""Construct connected, measured social-campaign waves from internal source assets."""

import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import shutil
import sys

from integrations.internal_mcp.io import materialize_skills, read_json, write_json


KEYS = {"workspace": "workspace_id", "user": "user_id", "social_account": "social_account_id", "campaign": "campaign_id",
    "content_post": "post_id", "asset": "asset_id", "audience_segment": "segment_id", "hashtag": "hashtag_id",
    "approval_request": "approval_request_id", "comment": "comment_id", "engagement_snapshot": "snapshot_id",
    "ab_test": "ab_test_id", "sync_job": "sync_job_id", "automation_rule": "rule_id", "response_template": "template_id"}
FORMAT = "skillhorizon.c06.measured_waves.v1"
SKILLS = Path(__file__).with_name("skills")


def build(source, output, *, seed=6, campaigns=2, waves=2, background=True, exposure_network=False, variants_per_brief=2):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("Use a fresh isolated output")
    minimum_waves = 2 if exposure_network else 1
    minimum_campaigns = 2 if exposure_network else 1
    if type(seed) is not int or type(campaigns) is not int or not minimum_campaigns <= campaigns <= 3 or type(waves) is not int or not minimum_waves <= waves <= 64:
        raise ValueError(f"Use integer seed, {minimum_campaigns}..3 campaigns and {minimum_waves}..64 waves")
    if type(variants_per_brief) is not int or not 2<=variants_per_brief<=5 or (exposure_network and variants_per_brief!=2):
        raise ValueError("Measured comparisons need2..5 variants; exposure retains exactly2")
    original = {kind: read_json(source / f"data/database/{kind}s.json") for kind in KEYS}
    rng = random.Random(seed)
    task_id = f"c06-{'exposure' if exposure_network else 'social'}-{seed}-{campaigns}c-{waves}w"
    if variants_per_brief!=2:
        task_id+=f"-{variants_per_brief}v"
    tables = {kind: [] for kind in KEYS}
    origin = datetime(2026, 10, 8, tzinfo=timezone.utc)

    def timestamp(hours):
        return (origin + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")

    def add(kind, identifier, values):
        row = {**deepcopy(rng.choice(original[kind])), **values, KEYS[kind]: identifier, "task_id": task_id}
        row.pop("id", None)
        tables[kind].append(row)
        return row

    workspace = task_id + "-workspace"
    editor, reviewer = task_id + "-coordinator", task_id + "-reviewer"
    add("workspace", workspace, {"name": "Connected campaign studio", "status": "active", "timezone": "UTC",
        "active_post_count": (variants_per_brief+1) * campaigns * waves, "pending_approval_count": 0, "scheduled_post_count": 0})
    for identifier, role in ((editor, "social_manager"), (reviewer, "approver")):
        add("user", identifier, {"workspace_id": workspace, "full_name": role.title(), "role": role, "status": "active",
            "email_address": role + "@synthetic.invalid", "authored_post_count": (variants_per_brief+1) * campaigns * waves if identifier == editor else 0, "open_approval_count": 0})
    accounts = []
    for index, platform in enumerate(("linkedin", "facebook")):
        identifier = f"{task_id}-account-{index}"
        add("social_account", identifier, {"workspace_id": workspace, "platform": platform, "handle": f"@synthetic-{index}",
            "status": "connected", "timezone": "UTC", "post_count": 0, "scheduled_count": 0, "published_count": 0, "max_body_characters": 2000})
        accounts.append(identifier)
    segments = []
    for index in range(1 + campaigns * variants_per_brief):
        identifier = f"{task_id}-audience-{index}"
        add("audience_segment", identifier, {"workspace_id": workspace, "name": "Consenting audience " + str(index), "status": "active",
            "criteria": {"platforms": ["linkedin", "facebook"], "regions": ["global"], "consented": True}, "estimated_size": 10000})
        segments.append(identifier)
    assets = []
    campaign_ids = []
    for lane in range(campaigns):
        campaign_id = f"{task_id}-campaign-{lane}"
        add("campaign", campaign_id, {"workspace_id": workspace, "name": f"Campaign {lane + 1}", "status": "active", "owner_user_id": editor,
            "start_at": timestamp(0), "end_at": timestamp(waves * 48), "post_count": (variants_per_brief+1) * waves, "scheduled_count": 0, "published_count": 0,
            "approval_pending_count": 0})
        campaign_ids.append(campaign_id)
        lane_assets = []
        for edition in range(2):
            identifier = f"{task_id}-asset-{lane}-edition{edition}"
            add("asset", identifier, {"workspace_id": workspace, "uploaded_by_user_id": editor, "status": "ready", "version": 1,
                "asset_type": "image", "title": f"Campaign {lane + 1} image edition {edition}", "campaign_id": campaign_id,
                "rights_expires_at": timestamp(waves * 48 + 72), "attached_post_count": 0, "storage_key": "synthetic-metadata-only/" + identifier})
            lane_assets.append(identifier)
        assets.append(lane_assets)
    briefs, responses = {}, {}
    for wave in range(waves):
        for lane, campaign_id in enumerate(campaign_ids):
            brief_id = f"{task_id}-wave{wave + 1}-campaign{lane}"
            parents = [] if not wave else ([f"{task_id}-wave{wave}-campaign{i}" for i in range(min(2,campaigns))] if lane == campaigns - 1
                else [f"{task_id}-wave{wave}-campaign{lane}"])
            variant_ids = [brief_id + f"-variant{i}" for i in range(variants_per_brief)]
            deployment_id = brief_id + "-deployment"
            brief = {"brief_id": brief_id, "campaign_id": campaign_id, "wave": wave + 1, "revision": 1,
                "product_fact": f"Product {lane + 1} offer: {19 + lane * 10} USD.", "disclosure": "Sponsored. Terms apply.",
                "ctas": [f"{verb} package {lane + 1} wave {wave + 1}" for verb in ("Explore","Compare","Review","Discover","Assess")[:variants_per_brief]],
                "parent_brief_ids": parents, "variant_post_ids": variant_ids, "deployment_post_id": deployment_id,
                "eligible_asset_ids": assets[lane], "allowed_audience_ids": segments,
                "initial_variant_audiences": [[segments[0], segments[1 + lane * variants_per_brief + v]] for v in range(variants_per_brief)],
                "variant_window": {"start": timestamp(wave * 48 + 1), "end": timestamp(wave * 48 + 18)},
                "deployment_window": {"start": timestamp(wave * 48 + 22), "end": timestamp(wave * 48 + 40)},
                "observation_hours": 2, "metric": "clicks_per_impression", "tie_rule": "lexicographically_smallest_post_id",
                "learning_policy": "Use exactly the prior winner reports. Select the highest clicks/impressions parent (ties by report ID); "
                    "inherit every parent winner CTA in the text and use the intersection of winner audiences and local allowed audiences."}
            briefs[brief_id] = brief
            for variant, post_id in enumerate(variant_ids + [deployment_id]):
                add("content_post", post_id, {"workspace_id": workspace, "campaign_id": campaign_id,
                    "social_account_id": accounts[wave % 2 if variant < variants_per_brief else (wave + 1) % 2], "author_user_id": editor,
                    "brief_id": brief_id, "role": "variant" if variant < variants_per_brief else "deployment", "variant_index": variant if variant < variants_per_brief else None,
                    "title": f"Campaign {lane + 1} wave {wave + 1} " + (f"variant {variant}" if variant < variants_per_brief else "deployment"),
                    "body": "Draft awaiting campaign reconciliation.", "content_format": "single_image", "version": 1,
                    "status": "draft", "approval_status": "not_requested", "asset_ids": [assets[lane][0]],
                    "audience_segment_ids": [], "hashtag_ids": [], "learning": None,
                    "scheduled_for": None, "published_at": None, "approved_request_id": None,
                    "platform_status": {"remote_post_id": None, "sync_status": "not_synced", "last_error": None},
                    "created_at": timestamp(0), "updated_at": timestamp(0)})
            metrics = [{"impressions": 10000, "clicks": rng.randint(250, 450)}, {"impressions": 1000, "clicks": rng.randint(60, 95)}]
            metrics.extend({"impressions":5000+1000*i,"clicks":rng.randint(100,600)} for i in range(variants_per_brief-2))
            rng.shuffle(metrics)
            responses.update({post_id: metric for post_id, metric in zip(variant_ids, metrics)})
    for account in tables["social_account"]:
        account["post_count"] = sum(p["social_account_id"] == account["social_account_id"] for p in tables["content_post"])
    for asset in tables["asset"]:
        asset["attached_post_count"] = sum(asset["asset_id"] in p["asset_ids"] for p in tables["content_post"])
    update = {"event_id": task_id + "-rights-withdrawal", "asset_id": assets[0][0],
        "reason": "The campaign-zero legacy image license was withdrawn for future publication.", "after_wave": 1}
    skills = [{"name": p.name, "files": [str(f.relative_to(p)) for f in sorted(p.rglob("*")) if f.is_file() and f.suffix in {".md", ".py"}]}
        for p in sorted(SKILLS.iterdir()) if p.is_dir()] if SKILLS.exists() else []
    public = {"format": FORMAT, "task_id": task_id, "workspace_id": workspace, "editor_user_id": editor, "reviewer_user_id": reviewer,
        "goal": "Deliver every connected campaign wave: prepare and approve all declared alternatives, publish within shared account slots, "
            "observe their performance, select the actual click-through-rate winner and publish its deployment. Carry measured prior learning "
            "into later waves and handle the declared post-wave-one rights update. Preserve historical publications and observations.",
        "brief_ids": list(briefs), "skills": skills, "clock_origin": timestamp(0), "clock_limit": timestamp(waves * 48),
        "minimum_account_gap_minutes": 60, "planned_updates": 1,
        "policy": {"skill_protocol": "observational_v1", "review": "synthetic_structural_content_not_human_quality",
            "analytics": "environment_generated_synthetic_not_real_marketing_lift", "assets": "metadata_only_no_image_generation",
            "published_history_immutable": True, "initial_posts_are_assigned_drafts_not_open_ended_campaign_creation": True}}
    world = {"format": FORMAT, "task_id": task_id, "tables": tables, "sequence": 0, "clock": timestamp(0),
        "social": {"next_id": 1, "briefs": briefs, "response_profiles": responses, "reports": {}, "publications": {}, "events": [],
            "revision_history": {}, "scheduled_updates": [update], "applied_updates": [], "skill_runs": {}, "active_skill_run": None}}
    write_json(output / "background.json", original if background else {kind: [] for kind in KEYS})
    write_json(output / "public/task.json", public)
    write_json(output / "checkpoint/world.json", world)
    write_json(output / "private/initial_obligations.json", {"public": public, "initial_tables": tables, "briefs": briefs,
        "response_profiles": responses, "scheduled_updates": [update]})
    write_json(output / "controller.json", {"source": str(source), "provider_calls": 0, "qualification": "UNVERIFIED"})
    shutil.copytree(output / "checkpoint", output / "runtime")
    if SKILLS.exists():
        materialize_skills(SKILLS, output / "public/skills")
    write_json(output / "mcp.json", {"mcpServers": {"c06-social": {"command": sys.executable,
        "args": ["-m", "integrations.socialops.server", str(output), str(output / "runtime")], "cwd": str(Path(__file__).resolve().parents[2])}}})
    return public


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=6)
    parser.add_argument("--campaigns", type=int, default=2)
    parser.add_argument("--waves", type=int, default=2)
    args = parser.parse_args()
    print(build(args.source, args.output, seed=args.seed, campaigns=args.campaigns, waves=args.waves)["task_id"])
