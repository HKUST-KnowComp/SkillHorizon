"""Independent campaign-state grading from initial obligations and actual records."""

from datetime import datetime, timedelta
from fractions import Fraction
from pathlib import Path

from integrations.internal_mcp.io import read_json


EVALUATOR_VERSION = "socialops_audience_set_v2"


def exact_variant_set(actual, expected):
    return len(actual) == len(expected) == len(set(expected)) and set(actual) == set(expected)


def evaluate(task, runtime):
    task, runtime = Path(task), Path(runtime)
    spec = read_json(task / "private/initial_obligations.json")
    world = read_json(runtime / "world.json")
    tables, state = world["tables"], world["social"]
    posts = {p["post_id"]: p for p in tables["content_post"]}
    initial_posts = {p["post_id"]: p for p in spec["initial_tables"]["content_post"]}
    approvals = {r["approval_request_id"]: r for r in tables["approval_request"]}
    observations = {r["snapshot_id"]: r for r in tables["engagement_snapshot"]}
    tests = {r["ab_test_id"]: r for r in tables["ab_test"]}
    assets = {a["asset_id"]: a for a in tables["asset"]}
    accounts = {a["social_account_id"]: a for a in tables["social_account"]}
    global_errors = []
    response_profiles = spec["response_profiles"]
    exposure_metrics = {}
    if "exposure_policy" in spec["public"]:
        from .exposure_evaluate import account
        if "reach_policy" in spec["public"]:
            from .reach_evaluate import account
        response_profiles, exposure_errors, exposure_metrics = account(spec, world)
        global_errors.extend(exposure_errors)
        if read_json(task / "public/task.json") != spec["public"]:
            global_errors.append("EXPOSURE_PUBLIC_POLICY_CHANGED")

    def date(value):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError("Timezone absent")
        return result

    def ratio(row):
        return Fraction(row["clicks"], row["impressions"]) if row["impressions"] else Fraction(0)

    def content(post):
        return {k: post[k] for k in ("post_id", "brief_id", "social_account_id", "campaign_id", "title", "body", "content_format",
            "asset_ids", "audience_segment_ids", "hashtag_ids", "learning", "version")}

    if world["task_id"] != spec["public"]["task_id"] or set(posts) != set(initial_posts) or len(posts) != len(tables["content_post"]):
        global_errors.append("TASK_POST_IDENTITY")
    if state["briefs"] != spec["briefs"] or state["response_profiles"] != spec["response_profiles"]:
        global_errors.append("IMMUTABLE_BUSINESS_INPUT_CHANGED")
    if [r["event_id"] for r in state["applied_updates"]] != [r["event_id"] for r in spec["scheduled_updates"]]:
        global_errors.append("PLANNED_UPDATES_INCOMPLETE")
    for update in spec["scheduled_updates"]:
        applied = [r for r in state["applied_updates"] if r["event_id"] == update["event_id"]]
        if applied:
            events = [e for e in state["events"] if e["event_type"] == "asset.rights_withdrawn" and e["entity_id"] == update["asset_id"]]
            if len(events) != 1 or assets[update["asset_id"]]["status"] != "withdrawn":
                global_errors.append("RIGHTS_UPDATE_EVIDENCE")
            if spec['public'].get('rights_update_trigger') and len(events)==1:
                trigger = spec['public']['rights_update_trigger']
                publication_events = sorted((e for e in state['events'] if e['event_type']=='post.published'), key=lambda e:e['sequence'])
                milestones = [e for e in publication_events if posts[e['entity_id']]['role']=='variant'
                    and spec['briefs'][posts[e['entity_id']]['brief_id']]['wave']==trigger['wave']]
                required = trigger['variant_publications']
                if len(milestones)<required or events[0]['sequence']<=milestones[required-1]['sequence']:
                    global_errors.append('RIGHTS_UPDATE_PROGRESS')
                elif any(milestones[required-1]['sequence'] < e['sequence'] < events[0]['sequence'] for e in publication_events):
                    global_errors.append('PUBLICATION_BEFORE_REQUIRED_NOTICE')
    for asset in tables["asset"]:
        if asset["attached_post_count"] != sum(asset["asset_id"] in p["asset_ids"] for p in posts.values()):
            global_errors.append("ATTACHMENT_COUNT")
    published = [p for p in posts.values() if p["status"] == "published"]
    for i, a in enumerate(published):
        for b in published[i + 1:]:
            if a["social_account_id"] == b["social_account_id"] and abs((date(a["published_at"]) - date(b["published_at"])).total_seconds()) < 3600:
                global_errors.append("ACCOUNT_PUBLICATION_COLLISION")

    outcomes = []
    for brief_id, brief in spec["briefs"].items():
        errors = []

        def check(condition, code):
            if not condition:
                errors.append(code)

        try:
            reports = [r for r in state["reports"].values() if r["brief_id"] == brief_id]
            check(len(reports) == 1, "EXACTLY_ONE_COMPLETED_REPORT")
            report = reports[0]
            test = tests[report["ab_test_id"]]
            check(test["brief_id"] == brief_id and test["status"] == "completed" and exact_variant_set(test["variant_post_ids"], brief["variant_post_ids"]), "EXPERIMENT_IDENTITY")
            obs = [observations[i] for i in report["snapshot_ids"]]
            check(exact_variant_set([r["post_id"] for r in obs], brief["variant_post_ids"]), "OBSERVATION_SET")
            for row in obs:
                post = posts[row["post_id"]]
                profile = response_profiles[post["post_id"]]
                check(row["clicks"] == profile["clicks"] and row["impressions"] == profile["impressions"], "OBSERVATION_METRICS")
                check(row["post_version"] == post["version"] and row["publication_id"] == post["publication_id"] and row["social_account_id"] == post["social_account_id"], "OBSERVATION_LINEAGE")
                check(row["source"] == "environment_synthetic_response" and row["observed_from"] == post["published_at"] and
                    date(row["observed_until"]) - date(row["observed_from"]) == timedelta(hours=brief["observation_hours"]), "OBSERVATION_INTERVAL")
                check(date(row["created_at"]) >= date(row["observed_until"]) and date(report["completed_at"]) >= date(row["created_at"]), "OBSERVATION_CAUSAL_TIME")
                matching = [e for e in state["events"] if e["event_type"] == "analytics.observed" and e["entity_id"] == row["snapshot_id"]]
                check(len(matching) == 1, "OBSERVATION_EVENT")
            ranked = sorted(brief["variant_post_ids"], key=lambda p: (-ratio(response_profiles[p]), p))
            winner = posts[ranked[0]]
            check(report["winning_post_id"] == test["winning_post_id"] == winner["post_id"], "NORMALIZED_WINNER")
            check(report["winner_content"] == content(winner), "WINNER_CONTENT")
            winning_observation = next(r for r in obs if r["post_id"] == winner["post_id"])
            check(report["winner_observation"] == winning_observation, "WINNER_OBSERVATION")
            check(report["observations"] == obs or sorted(report["observations"], key=lambda r: r["snapshot_id"]) == sorted(obs, key=lambda r: r["snapshot_id"]), "REPORT_OBSERVATION_COPY")
            check(report["winning_cta"] == brief["ctas"][winner["variant_index"]], "WINNER_CTA")
            for identifier in brief["variant_post_ids"] + [brief["deployment_post_id"]]:
                post = posts[identifier]
                initial = initial_posts[identifier]
                check(all(post[k] == initial[k] for k in ("post_id", "brief_id", "campaign_id", "social_account_id", "author_user_id", "role", "variant_index")), "POST_BUSINESS_IDENTITY")
                check(post["status"] == "published" and post["approval_status"] == "approved", "POST_NOT_PUBLISHED")
                publication = state["publications"][post["publication_id"]]
                check(publication["content"] == content(post) and publication["post_version"] == post["version"], "PUBLICATION_CONTENT")
                check(publication["published_at"] == post["published_at"] and publication["post_id"] == identifier, "PUBLICATION_IDENTITY")
                window = brief["variant_window"] if post["role"] == "variant" else brief["deployment_window"]
                check(date(window["start"]) <= date(post["scheduled_for"]) <= date(post["published_at"]) <= date(window["end"]), "RELEASE_WINDOW")
                review = approvals[publication["approval_request_id"]]
                check(review["content_snapshot"] == content(post) and review["status"] == review.get("environment_decision") == "approved", "VERSIONED_APPROVAL")
                check(review["assigned_reviewer_user_id"] == spec["public"]["reviewer_user_id"] and review["requested_by_user_id"] == spec["public"]["editor_user_id"], "REVIEWER_IDENTITY")
                events = [e for e in state["events"] if e["event_type"] == "approval.environment_decision" and e["entity_id"] == review["approval_request_id"]]
                check(len(events) == 1 and events[0]["details"]["content_snapshot"] == content(post) and events[0]["details"]["decision"] == "approved", "REVIEW_EVENT")
                check(brief["product_fact"] in post["body"] and brief["disclosure"] in post["body"], "PUBLIC_CONTENT_OBLIGATIONS")
                check(len(post["body"]) <= accounts[post["social_account_id"]]["max_body_characters"] and post["content_format"] == "single_image", "PLATFORM_FORMAT")
                evidence = publication["asset_evidence"]
                check(bool(evidence) and [a["asset_id"] for a in evidence] == post["asset_ids"], "ASSET_EVIDENCE_SET")
                check(all(a["asset_id"] in brief["eligible_asset_ids"] and a["status"] == "ready" and date(a["rights_expires_at"]) >= date(post["published_at"]) for a in evidence), "ASSET_RIGHTS_AT_RELEASE")
                for applied in state["applied_updates"]:
                    if applied["asset_id"] in post["asset_ids"]:
                        check(date(post["published_at"]) <= date(applied["released_at"]), "WITHDRAWN_ASSET_PUBLISHED")
                binding = post["learning"]
                parents = [state["reports"][i] for i in binding["report_ids"]]
                needed = [brief_id] if post["role"] == "deployment" else brief["parent_brief_ids"]
                check(len(parents) == len(needed) and {r["brief_id"] for r in parents} == set(needed), "PARENT_REPORT_SET")
                if parents:
                    selected = sorted(parents, key=lambda r: (-ratio(r["winner_observation"]), r["report_id"]))[0]
                    check(binding["selected_parent_report_id"] == selected["report_id"], "PARENT_RATE_SELECTION")
                    audience = set(brief["allowed_audience_ids"])
                    for parent in parents:
                        audience &= set(parent["winner_content"]["audience_segment_ids"])
                        check(date(parent["completed_at"]) <= date(review["requested_at"]), "LEARNING_CAUSAL_TIME")
                    if spec['public']['policy'].get('audience_learning') == 'campaign_consent_renewal_v2' and post['role'] == 'variant':
                        arm = post['variant_index']
                        parent_union = {s for parent in parents for s in parent['winner_content']['audience_segment_ids']}
                        audience = (parent_union & set(brief['variant_audience_permissions'][arm])) | set(brief['newly_consented_audiences'][arm])
                    check(all(p["winning_cta"] in post["body"] for p in parents), "INHERITED_WINNER_CONTENT")
                else:
                    check(not binding["selected_parent_report_id"], "UNEXPECTED_PARENT")
                    audience = set(brief["initial_variant_audiences"][post["variant_index"]])
                check(set(post["audience_segment_ids"]) == set(binding["audience_segment_ids"]) == audience and bool(audience), "AUDIENCE_INTERSECTION")
                if post["role"] == "deployment":
                    same_asset = post['asset_ids'] == winner['asset_ids']
                    if spec['public']['policy'].get('deployment_asset') == 'same_creative_current_license_v2':
                        same_asset = sorted(assets[a]['creative_id'] for a in post['asset_ids']) == sorted(assets[a]['creative_id'] for a in winner['asset_ids'])
                    # Audience membership is unordered; content snapshots above remain exact.
                    same_audience = exact_variant_set(post["audience_segment_ids"], winner["audience_segment_ids"])
                    check(post["body"] == winner["body"] and same_audience and same_asset, "DEPLOY_ACTUAL_WINNER")
                else:
                    check(brief["ctas"][post["variant_index"]] in post["body"], "VARIANT_CTA")
        except (KeyError, ValueError, TypeError, IndexError, StopIteration):
            errors.append("MISSING_OR_MALFORMED_EVIDENCE")
        outcomes.append({"brief_id": brief_id, "accepted": not errors, "errors": sorted(set(errors))})
    return {"task_id": world["task_id"], "evaluator_version": EVALUATOR_VERSION,
        "accepted": not global_errors and all(r["accepted"] for r in outcomes),
        "completed_briefs": sum(r["accepted"] for r in outcomes), "required_briefs": len(outcomes),
        "published_posts": len(published), "required_posts": len(initial_posts), "global_errors": sorted(set(global_errors)), "briefs": outcomes,
        "provider_calls": 0, "evidence_kind": "zero_model_business_state_not_model_behavior",
        **({"exposure_metrics": exposure_metrics} if exposure_metrics else {})}
