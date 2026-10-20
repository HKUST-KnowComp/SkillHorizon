"""Transactional social publishing with version-bound review and observed analytics."""

from contextlib import ExitStack
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from fractions import Fraction
import json
from pathlib import Path
import sys
from unittest.mock import patch

import jsonschema

from integrations.internal_mcp.io import load_backend, read_json, write_json, controller_source
from integrations.internal_mcp.tables import TableState
from integrations.internal_mcp.parameter_contract import apply_public_enums
from .build import KEYS


READS = {"list_workspaces", "list_social_accounts", "get_social_account_profile", "search_content_posts", "get_content_post_detail",
    "list_campaigns", "list_audience_segments", "list_hashtags", "get_engagement_summary", "list_comments", "list_approval_requests"}


class BusinessError(Exception):
    pass


def require(condition, code):
    if not condition:
        raise BusinessError(code)


def schema(properties, required=None):
    return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}


def instant(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        require(result.tzinfo is not None, "TIMEZONE_REQUIRED")
        return result.astimezone(timezone.utc)
    except (AttributeError, ValueError):
        raise BusinessError("INVALID_TIMESTAMP") from None


def stamp(value):
    return value.isoformat().replace("+00:00", "Z")


def rate(row):
    return Fraction(row["clicks"], row["impressions"]) if row["impressions"] else Fraction(0)


class Social:
    def __init__(self, task, runtime):
        self.task, self.runtime = Path(task).resolve(), Path(runtime).resolve()
        self.source = controller_source(self.task)
        if self.runtime.is_relative_to(self.source) or self.source.is_relative_to(self.runtime):
            raise ValueError("Use an isolated runtime")
        self.backend = load_backend(self.source)
        self.public = read_json(self.task / "public/task.json")
        self.world = read_json(self.runtime / "world.json")
        require(self.world["task_id"] == self.public["task_id"], "TASK_RUNTIME_MISMATCH")
        self.sm = TableState(self.world, read_json(self.task / "background.json"), KEYS)
        self.metadata = {name: deepcopy(self.backend._TOOL_METADATA[name]) for name in sorted(READS)}
        for name, metadata in self.metadata.items():
            inputs = metadata["input_schema"]
            inputs["additionalProperties"] = False
            if (name.startswith("list_") and name != "list_workspaces") or name == "search_content_posts":
                inputs["properties"].update({"offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 100}})
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 30}
        self.metadata["get_content_post_detail"]["input_schema"]["properties"].update({"include_comments": {"type": "boolean"}, "include_engagement_history": {"type": "boolean"}})
        self.metadata["search_content_posts"]["input_schema"]["properties"].update({k: text for k in ("social_account_id", "campaign_id", "status_filter", "query")})
        definitions = {
            "read_campaign_brief": ("Read a campaign-wave brief, eligible asset metadata and actual current clock; no future metrics.", schema({"brief_id": text})),
            "read_approval": ("Read a revision-bound review and actual environment decision.", schema({"approval_request_id": text})),
            "read_campaign_report": ("Read an actual completed experiment report with observations and winner.", schema({"report_id": text})),
            "list_campaign_reports": ("List completed report history, optionally by brief.", schema({"brief_id": text, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 30}}, [])),
            "read_post_publication": ("Read immutable published content and approval/learning evidence.", schema({"publication_id": text})),
            "update_content_post": ("Revise an unpublished draft. Content changes invalidate approval and release any future reservation.", schema({"post_id": text, "title": text,
                "body": text, "asset_ids": ids, "audience_segment_ids": ids, "hashtag_ids": ids}, ["post_id"])),
            "attach_asset_to_post": ("Attach one eligible ready asset to an unpublished task post; invalidates prior approval.", schema({"post_id": text, "asset_id": text}, ["post_id", "asset_id"])),
            "bind_campaign_learning": ("Validate the actor's measured parent selection and computed consenting-audience intersection. Does not return a corrected answer on failure.", schema({"post_id": text, "report_ids": ids,
                "selected_parent_report_id": {"type": "string"}, "audience_segment_ids": ids}, ["post_id", "report_ids", "audience_segment_ids"])),
            "request_post_approval": ("Submit current content to the declared reviewer; only final approval stage is admitted.", schema({"post_id": text,
                "requested_by_user_id": text, "assigned_reviewer_user_id": text, "stage": {"enum": ["final_approval"]}, "request_note": {"type": "string"}},
                ["post_id", "requested_by_user_id", "assigned_reviewer_user_id", "stage"])),
            "run_content_review": ("Execute environment-owned campaign fact, disclosure, learning, audience, asset and format checks.", schema({"approval_request_id": text})),
            "schedule_post": ("Reserve a release time within the public window, with one-hour same-account spacing.", schema({"post_id": text, "scheduled_for": text})),
            "cancel_scheduled_post": ("Release an unpublished reservation and return the post to its current approval state.", schema({"post_id": text})),
            "publish_post": ("Publish one actually due reserved post with current approval. Remote identity is environment generated.", schema({"post_id": text})),
            "advance_campaign_clock": ("Advance bounded simulated business time; never auto-publish or backdate posts.", schema({"to": text})),
            "observe_post": ("Collect the environment's fixed two-hour observation of a published variant; metrics are not actor writable.", schema({"post_id": text})),
            "launch_ab_test": ("Start the brief's exact variant comparison after publication, using clicks/impressions.", schema({"brief_id": text, "variant_post_ids": ids})),
            "complete_campaign_experiment": ("Propose the measured winner using the exact observation set. Checks identity, exposure and normalized metric.", schema({"ab_test_id": text, "snapshot_ids": ids, "winning_post_id": text})),
            "poll_campaign_updates": ("Poll the declared post-wave-one rights update; published history is not rewritten.", schema({})),
        }
        self.metadata.update({n: {"description": d, "input_schema": s} for n, (d, s) in definitions.items()})
        apply_public_enums("C06", self.metadata)
        self.metadata["list_workspaces"]["description"] = "Read the initial workspace summary page; this benchmark view does not expose optional workspace filters."
        self.validate()

    @property
    def state(self):
        return self.world["social"]

    def now(self):
        return self.world["clock"]

    def owned(self, kind, identifier):
        row = self.sm.get_entity(kind, identifier)
        require(row is not None, kind.upper() + "_NOT_FOUND")
        require(row.get("task_id") == self.world["task_id"], "OUTSIDE_TASK_SCOPE")
        return row

    def update(self, kind, identifier, values):
        return self.sm.update_entity(kind, identifier, values)

    def next_id(self, kind):
        value = self.state["next_id"]
        self.state["next_id"] += 1
        from integrations.internal_mcp.topology import runtime_identifier
        return runtime_identifier(self.public, kind, value, f"{self.world['task_id']}-{kind}-{value}")

    def event(self, kind, identifier, details):
        self.state["events"].append({"event_id": self.next_id("event"), "event_type": kind, "entity_id": identifier,
            "sequence": self.world["sequence"], "occurred_at": self.now(), "details": deepcopy(details)})

    def native(self, name, args):
        with ExitStack() as stack:
            for module_name, module in tuple(sys.modules.items()):
                if module_name.startswith("mcp_servers.") and hasattr(module, "now_iso"):
                    stack.enter_context(patch.object(module, "now_iso", self.now))
            output = self.backend.call_tool(name, state_manager=self.sm, **args)["content"]
        if output.get("message", "").startswith("INTERNAL_ERROR"):
            raise RuntimeError(output["message"])
        require(output["status"] == "success", output.get("message", "NATIVE_TOOL_ERROR"))
        return output["data"]

    def brief(self, identifier):
        require(identifier in self.state["briefs"], "BRIEF_NOT_FOUND")
        return deepcopy(self.state["briefs"][identifier])

    def snapshot(self, post_id):
        post = self.owned("content_post", post_id)
        return {k: post[k] for k in ("post_id", "brief_id", "social_account_id", "campaign_id", "title", "body", "content_format",
            "asset_ids", "audience_segment_ids", "hashtag_ids", "learning", "version")}

    def report(self, identifier):
        require(identifier in self.state["reports"], "REPORT_NOT_FOUND")
        return deepcopy(self.state["reports"][identifier])

    def learning(self, post, report_ids, selected):
        brief = self.brief(post["brief_id"])
        reports = [self.report(i) for i in report_ids]
        needed = [brief["brief_id"]] if post["role"] == "deployment" else brief["parent_brief_ids"]
        require(len(reports) == len(needed) and {r["brief_id"] for r in reports} == set(needed), "LEARNING_REPORT_SET")
        if reports:
            best = sorted(reports, key=lambda r: (-rate(r["winner_observation"]), r["report_id"]))[0]
            require(selected == best["report_id"], "WRONG_MEASURED_PARENT")
            audiences = set(brief["allowed_audience_ids"])
            for row in reports:
                audiences &= set(row["winner_content"]["audience_segment_ids"])
            if self.public['policy'].get('audience_learning') == 'campaign_consent_renewal_v2' and post['role'] == 'variant':
                arm = post['variant_index']
                audiences = set.union(*(set(row['winner_content']['audience_segment_ids']) for row in reports))
                audiences &= set(brief['variant_audience_permissions'][arm])
                audiences |= set(brief['newly_consented_audiences'][arm])
            require(audiences, "EMPTY_CONSENTED_AUDIENCE_INTERSECTION")
        else:
            require(not selected, "UNEXPECTED_PARENT_SELECTION")
            audiences = set(brief["initial_variant_audiences"][post["variant_index"]])
        return {"report_ids": sorted(report_ids), "selected_parent_report_id": selected or "", "audience_segment_ids": sorted(audiences)}

    def invalidate(self, post_id, previous):
        post = self.owned("content_post", post_id)
        require(post["status"] != "published", "PUBLISHED_CONTENT_IMMUTABLE")
        self.state["revision_history"].setdefault(post_id, []).append(previous)
        self.update("content_post", post_id, {"version": previous["version"] + 1, "status": "draft", "approval_status": "not_requested",
            "approved_request_id": None, "scheduled_for": None, "updated_at": self.now()})
        for review in self.world["tables"]["approval_request"]:
            if review["post_id"] == post_id and review["status"] != "canceled":
                self.update("approval_request", review["approval_request_id"], {"prior_status": review["status"], "status": "canceled"})
        self.event("post.revised", post_id, {"previous_version": previous["version"], "new_version": previous["version"] + 1})

    def checks(self, post_id):
        post = self.owned("content_post", post_id)
        brief = self.brief(post["brief_id"])
        account = self.owned("social_account", post["social_account_id"])
        results = []

        def check(rule, passed):
            results.append({"rule": rule, "passed": bool(passed)})

        check("CAMPAIGN_FACT", brief["product_fact"] in post["body"])
        check("DISCLOSURE", brief["disclosure"] in post["body"])
        check("PLATFORM_FORMAT", post["content_format"] == "single_image" and len(post["body"]) <= account["max_body_characters"])
        assets = [self.owned("asset", a) for a in post["asset_ids"]]
        check("ASSET_ELIGIBILITY", bool(assets) and all(a["asset_id"] in brief["eligible_asset_ids"] and a["status"] == "ready"
            and instant(a["rights_expires_at"]) >= instant(brief["deployment_window"]["end"]) for a in assets))
        try:
            binding = post["learning"] or {}
            expected = self.learning(post, binding.get("report_ids", []), binding.get("selected_parent_report_id", ""))
            check("MEASURED_LEARNING", binding == expected)
            check("CONSENTED_AUDIENCE", set(post["audience_segment_ids"]) == set(expected["audience_segment_ids"]))
            reports = [self.report(i) for i in expected["report_ids"]]
            if post["role"] == "deployment":
                winner = reports[0]["winner_content"]
                same_assets = post['asset_ids'] == winner['asset_ids']
                if self.public['policy'].get('deployment_asset') == 'same_creative_current_license_v2':
                    same_assets = sorted(self.owned('asset', a)['creative_id'] for a in post['asset_ids']) == sorted(
                        self.owned('asset', a)['creative_id'] for a in winner['asset_ids'])
                check("WINNER_DEPLOYMENT", post["body"] == winner["body"] and same_assets)
            else:
                check("VARIANT_CTA", brief["ctas"][post["variant_index"]] in post["body"])
                check("INHERITED_CTA", all(r["winning_cta"] in post["body"] for r in reports))
        except BusinessError:
            check("MEASURED_LEARNING", False)
        return results

    def current_approval(self, post_id):
        post = self.owned("content_post", post_id)
        review = self.owned("approval_request", post.get("approved_request_id"))
        require(review["post_id"] == post_id and review["status"] == "approved" and review["content_snapshot"] == self.snapshot(post_id), "STALE_APPROVAL")
        require(review.get("environment_decision") == "approved" and review["assigned_reviewer_user_id"] == self.public["reviewer_user_id"], "ACTUAL_APPROVAL_REQUIRED")
        require(all(c["passed"] for c in self.checks(post_id)), "CURRENT_CONTENT_REVIEW_FAILED")
        return review

    def recount(self):
        posts = self.world["tables"]["content_post"]
        for asset in self.world["tables"]["asset"]:
            self.update("asset", asset["asset_id"], {"attached_post_count": sum(asset["asset_id"] in p["asset_ids"] for p in posts)})
        for kind, key in (("social_account", "social_account_id"), ("campaign", "campaign_id")):
            for row in self.world["tables"][kind]:
                related = [p for p in posts if p[key] == row[key]]
                self.update(kind, row[key], {"post_count": len(related), "scheduled_count": sum(p["status"] == "scheduled" for p in related),
                    "published_count": sum(p["status"] == "published" for p in related),
                    **({"approval_pending_count": sum(p["approval_status"] == "pending" for p in related)} if kind == "campaign" else {})})
        for user in self.world["tables"]["user"]:
            self.update("user", user["user_id"], {"open_approval_count": sum(r["status"] == "pending" and r["assigned_reviewer_user_id"] == user["user_id"]
                for r in self.world["tables"]["approval_request"])})
        self.update("workspace", self.public["workspace_id"], {"active_post_count": len(posts), "scheduled_post_count": sum(p["status"] == "scheduled" for p in posts),
            "pending_approval_count": sum(r["status"] == "pending" for r in self.world["tables"]["approval_request"])})

    def dispatch(self, name, args):
        if name in READS:
            return self.native(name, args)
        if name == "read_campaign_brief":
            brief = self.brief(args["brief_id"])
            return {"brief": brief, "assets": [self.owned("asset", i) for i in brief["eligible_asset_ids"]], "clock": self.now()}
        if name == "read_approval":
            return self.owned("approval_request", args["approval_request_id"])
        if name == "read_campaign_report":
            return self.report(args["report_id"])
        if name == "list_campaign_reports":
            if args.get("brief_id"):
                self.brief(args["brief_id"])
            rows = [r for r in self.state["reports"].values() if not args.get("brief_id") or r["brief_id"] == args["brief_id"]]
            offset, limit = args.get("offset", 0), args.get("limit", 20)
            return {"total": len(rows), "offset": offset, "reports": deepcopy(rows[offset:offset + limit])}
        if name == "read_post_publication":
            require(args["publication_id"] in self.state["publications"], "PUBLICATION_NOT_FOUND")
            return deepcopy(self.state["publications"][args["publication_id"]])
        if name in {"update_content_post", "attach_asset_to_post", "bind_campaign_learning"}:
            post = self.owned("content_post", args["post_id"])
            require(post["status"] != "published", "PUBLISHED_CONTENT_IMMUTABLE")
            previous = self.snapshot(post["post_id"])
            for key, kind in (("asset_ids", "asset"), ("audience_segment_ids", "audience_segment"), ("hashtag_ids", "hashtag")):
                for identifier in args.get(key, []):
                    self.owned(kind, identifier)
            if name == "attach_asset_to_post":
                self.owned("asset", args["asset_id"])
            if name == "bind_campaign_learning":
                binding = self.learning(post, args["report_ids"], args.get("selected_parent_report_id", ""))
                require(set(args["audience_segment_ids"]) == set(binding["audience_segment_ids"]),
                    'WRONG_CURRENT_CONSENTED_AUDIENCE' if self.public['policy'].get('audience_learning') == 'campaign_consent_renewal_v2'
                    else "WRONG_CONSENTED_AUDIENCE_INTERSECTION")
                self.update("content_post", post["post_id"], {"learning": binding})
                result = {"learning": binding}
            else:
                result = self.native(name, args)
            if self.snapshot(post["post_id"]) != previous:
                self.invalidate(post["post_id"], previous)
            return {**result, "post_version": self.owned("content_post", post["post_id"])["version"]}
        if name == "request_post_approval":
            post = self.owned("content_post", args["post_id"])
            require(args["requested_by_user_id"] == self.public["editor_user_id"] and args["assigned_reviewer_user_id"] == self.public["reviewer_user_id"], "ACTOR_OR_REVIEWER_IDENTITY")
            result = self.native(name, args)
            self.update("approval_request", result["approval_request_id"], {"content_snapshot": self.snapshot(post["post_id"]), "environment_decision": None, "checks": []})
            return result
        if name == "run_content_review":
            review = self.owned("approval_request", args["approval_request_id"])
            require(review["status"] != "canceled" and review["content_snapshot"] == self.snapshot(review["post_id"]), "STALE_APPROVAL")
            if review.get("environment_decision"):
                return review
            checks = self.checks(review["post_id"])
            accepted = all(c["passed"] for c in checks)
            self.native("approve_post" if accepted else "reject_post", {"approval_request_id": review["approval_request_id"],
                "reviewer_user_id": self.public["reviewer_user_id"], "decision_note": "Environment structural review: " + ("passed" if accepted else ", ".join(c["rule"] for c in checks if not c["passed"]))})
            result = self.update("approval_request", review["approval_request_id"], {"environment_decision": "approved" if accepted else "rejected", "checks": checks})
            if accepted:
                self.update("content_post", review["post_id"], {"approved_request_id": review["approval_request_id"]})
            self.event("approval.environment_decision", review["approval_request_id"], {"reviewer_id": self.public["reviewer_user_id"], "decision": result["environment_decision"], "content_snapshot": review["content_snapshot"]})
            return result
        if name == "advance_campaign_clock":
            target = instant(args["to"])
            require(instant(self.now()) <= target <= instant(self.public["clock_limit"]), "CLOCK_RANGE")
            self.world["clock"] = stamp(target)
            return {"clock": self.now()}
        if name == "schedule_post":
            post = self.owned("content_post", args["post_id"])
            require(self.owned("social_account", post["social_account_id"])["status"] == "connected", "ACCOUNT_NOT_CONNECTED")
            require(post["status"] in {"approved", "scheduled"}, "POST_NOT_APPROVED")
            self.current_approval(post["post_id"])
            brief = self.brief(post["brief_id"])
            window = brief["variant_window"] if post["role"] == "variant" else brief["deployment_window"]
            target = instant(args["scheduled_for"])
            require(instant(self.now()) <= target and instant(window["start"]) <= target <= instant(window["end"]), "RELEASE_WINDOW")
            self.check_slot(post, target)
            return self.update("content_post", post["post_id"], {"status": "scheduled", "scheduled_for": stamp(target)})
        if name == "cancel_scheduled_post":
            post = self.owned("content_post", args["post_id"])
            require(post["status"] == "scheduled", "POST_NOT_SCHEDULED")
            return self.native(name, {"post_id": post["post_id"], "cancel_mode": "return_to_approved"})
        if name == "publish_post":
            return self.publish(args["post_id"])
        if name == "observe_post":
            return self.observe(args["post_id"])
        if name == "launch_ab_test":
            brief = self.brief(args["brief_id"])
            require(set(args["variant_post_ids"]) == set(brief["variant_post_ids"]), "VARIANT_SET")
            require(all(self.owned("content_post", i)["status"] == "published" for i in args["variant_post_ids"]), "VARIANTS_NOT_PUBLISHED")
            for test in self.world["tables"]["ab_test"]:
                if test.get("brief_id") == brief["brief_id"]:
                    return test
            result = self.native(name, {"campaign_id": brief["campaign_id"], "name": brief["brief_id"], "variant_post_ids": args["variant_post_ids"], "success_metric": "conversion_proxy", "start_at": self.now()})
            return self.update("ab_test", result["ab_test_id"], {"brief_id": brief["brief_id"], "success_metric": "clicks_per_impression"})
        if name == "complete_campaign_experiment":
            return self.complete(args)
        if name == "poll_campaign_updates":
            return self.poll_updates()
        raise BusinessError("TOOL_NOT_EXPOSED")

    def check_slot(self, post, target):
        for other in self.world["tables"]["content_post"]:
            if other["post_id"] != post["post_id"] and other["social_account_id"] == post["social_account_id"] and other["status"] in {"scheduled", "published"}:
                occupied = instant(other["published_at"] if other["status"] == "published" else other["scheduled_for"])
                require(abs((target - occupied).total_seconds()) >= 3600, "ACCOUNT_SLOT_CONFLICT")

    def publish(self, post_id):
        post = self.owned("content_post", post_id)
        if post["status"] == "published":
            return deepcopy(self.state["publications"][post["publication_id"]])
        require(post["status"] == "scheduled" and instant(post["scheduled_for"]) <= instant(self.now()), "POST_NOT_DUE")
        brief = self.brief(post["brief_id"])
        window = brief["variant_window"] if post["role"] == "variant" else brief["deployment_window"]
        require(instant(self.now()) <= instant(window["end"]), "RELEASE_DEADLINE_MISSED")
        if self.public.get('rights_update_trigger'):
            require(not self.update_due() or bool(self.state['applied_updates']), "DECLARED_UPDATE_NOT_POLLED")
        elif brief["wave"] > self.public.get("rights_update_after_wave", 1):
            require(len(self.state["applied_updates"]) == len(self.state["scheduled_updates"]), "DECLARED_UPDATE_NOT_POLLED")
        self.current_approval(post_id)
        self.check_slot(post, instant(self.now()))
        self.native("publish_post", {"post_id": post_id, "publish_mode": "scheduled_due", "remote_post_id": "synthetic-remote-" + post_id})
        identifier = self.next_id("publication")
        publication = {"publication_id": identifier, "post_id": post_id, "post_version": post["version"], "content": self.snapshot(post_id),
            "approval_request_id": post["approved_request_id"], "published_at": self.now(), "scheduled_for": post["scheduled_for"],
            "asset_evidence": [self.owned("asset", i) for i in post["asset_ids"]]}
        self.state["publications"][identifier] = publication
        self.update("content_post", post_id, {"publication_id": identifier})
        self.event("post.published", post_id, {"publication_id": identifier})
        return deepcopy(publication)

    def observe(self, post_id):
        post = self.owned("content_post", post_id)
        require(post["status"] == "published" and post["role"] == "variant", "PUBLISHED_VARIANT_REQUIRED")
        end = instant(post["published_at"]) + timedelta(hours=self.brief(post["brief_id"])["observation_hours"])
        require(instant(self.now()) >= end, "OBSERVATION_NOT_READY")
        for row in self.world["tables"]["engagement_snapshot"]:
            if row["post_id"] == post_id and row["post_version"] == post["version"]:
                return deepcopy(row)
        metrics = self.response_metrics(post_id)
        result = self.native("record_engagement_snapshot", {"post_id": post_id, "captured_at": stamp(end), **metrics,
            "likes": 0, "comments": 0, "shares": 0, "follower_count": self.owned("social_account", post["social_account_id"])["follower_count"]})
        row = self.update("engagement_snapshot", result["snapshot_id"], {"post_version": post["version"], "publication_id": post["publication_id"],
            "observed_from": post["published_at"], "observed_until": stamp(end), "source": "environment_synthetic_response"})
        self.event("analytics.observed", row["snapshot_id"], {"post_id": post_id, "publication_id": post["publication_id"]})
        return row

    def response_metrics(self, post_id):
        return self.state["response_profiles"][post_id]

    def complete(self, args):
        test = self.owned("ab_test", args["ab_test_id"])
        observations = [self.owned("engagement_snapshot", i) for i in args["snapshot_ids"]]
        require(len(observations) == len(test["variant_post_ids"]) and {r["post_id"] for r in observations} == set(test["variant_post_ids"]), "OBSERVATION_SET")
        for row in observations:
            post = self.owned("content_post", row["post_id"])
            require(row["post_version"] == post["version"] and row["publication_id"] == post["publication_id"] and row["source"] == "environment_synthetic_response", "OBSERVATION_LINEAGE")
        winner = sorted(observations, key=lambda r: (-rate(r), r["post_id"]))[0]
        require(args["winning_post_id"] == winner["post_id"], "WRONG_MEASURED_WINNER")
        if test["status"] == "completed":
            return self.report(test["report_id"])
        post = self.owned("content_post", winner["post_id"])
        brief = self.brief(test["brief_id"])
        identifier = self.next_id("report")
        report = {"report_id": identifier, "brief_id": brief["brief_id"], "ab_test_id": test["ab_test_id"],
            "snapshot_ids": sorted(args["snapshot_ids"]), "observations": observations, "winning_post_id": post["post_id"],
            "winner_observation": winner, "winner_content": self.snapshot(post["post_id"]), "winning_cta": brief["ctas"][post["variant_index"]],
            "metric": "clicks_per_impression", "completed_at": self.now()}
        self.state["reports"][identifier] = report
        self.update("ab_test", test["ab_test_id"], {"status": "completed", "ended_at": self.now(), "winning_post_id": post["post_id"], "report_id": identifier})
        self.event("experiment.completed", test["ab_test_id"], {"report_id": identifier})
        return deepcopy(report)

    def update_due(self):
        trigger = self.public.get('rights_update_trigger')
        if trigger:
            return sum(p['status']=='published' and p['role']=='variant'
                       and self.state['briefs'][p['brief_id']]['wave']==trigger['wave']
                       for p in self.world['tables']['content_post']) >= trigger['variant_publications']
        first = [b for b in self.state['briefs'].values()
                 if b['wave'] == self.public.get('rights_update_after_wave', 1)]
        return all(self.owned('content_post', b['deployment_post_id'])['status']=='published' for b in first)

    def poll_updates(self):
        if self.state["applied_updates"]:
            return {"status": "drained", "events": []}
        if not self.update_due():
            return {"status": "awaiting_release_progress" if self.public.get('rights_update_trigger') else "awaiting_wave_one_delivery", "events": []}
        update = self.state["scheduled_updates"][0]
        self.update("asset", update["asset_id"], {"status": "withdrawn", "updated_at": self.now()})
        affected, invalidated_approvals, released_reservations = [], [], []
        for post in self.world["tables"]["content_post"]:
            if update["asset_id"] in post["asset_ids"] and post["status"] != "published":
                if post.get('approved_request_id'):
                    invalidated_approvals.append(post['approved_request_id'])
                if post.get('scheduled_for'):
                    released_reservations.append(post['post_id'])
                self.invalidate(post["post_id"], self.snapshot(post["post_id"]))
                affected.append(post["post_id"])
        released = {**update, "released_at": self.now(), "affected_post_ids": affected}
        if self.public.get('rights_update_trigger'):
            released.update(invalidated_approval_ids=invalidated_approvals, released_reservation_post_ids=released_reservations)
        self.state["applied_updates"].append(released)
        self.event("asset.rights_withdrawn", update["asset_id"], released)
        return {"status": "update_released", "events": [released]}

    def validate(self):
        for kind, rows in self.world["tables"].items():
            require(len({r[KEYS[kind]] for r in rows}) == len(rows), "DUPLICATE_ENTITY_ID")
            require(all(r.get("task_id") == self.world["task_id"] for r in rows), "TASK_OWNERSHIP")
        for post in self.world["tables"]["content_post"]:
            require(type(post["version"]) is int and post["version"] >= 1, "CONTENT_VERSION")
            for key, kind in (("asset_ids", "asset"), ("audience_segment_ids", "audience_segment"), ("hashtag_ids", "hashtag")):
                require(len(set(post[key])) == len(post[key]), "DUPLICATE_POST_LINK")
                for identifier in post[key]:
                    self.owned(kind, identifier)
            self.owned("social_account", post["social_account_id"])
            require(post["campaign_id"] == self.brief(post["brief_id"])["campaign_id"], "CAMPAIGN_IDENTITY")
        for asset in self.world["tables"]["asset"]:
            require(asset["attached_post_count"] == sum(asset["asset_id"] in p["asset_ids"] for p in self.world["tables"]["content_post"]), "ATTACHMENT_COUNT")

    def record_call(self, name, arguments, result):
        pass

    def call(self, name, arguments):
        before = deepcopy(self.world)
        try:
            require(name in self.metadata, "TOOL_NOT_EXPOSED")
            try:
                json.dumps(arguments, allow_nan=False)
                jsonschema.validate(arguments, self.metadata[name]["input_schema"])
            except (ValueError, TypeError, jsonschema.ValidationError):
                raise BusinessError("ARGUMENT_SCHEMA") from None
            data = self.dispatch(name, deepcopy(arguments))
            if name not in READS:
                self.recount()
                self.validate()
            result = {"status": "success", "data": data, "message": ""}
        except BusinessError as error:
            self.world = before
            self.sm.bind(self.world)
            result = {"status": "error", "data": {}, "message": str(error)}
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        self.record_call(name, arguments, result)
        self.world["sequence"] += 1
        try:
            write_json(self.runtime / "world.json", self.world)
        except Exception:
            self.world = before
            self.sm.bind(self.world)
            raise
        return result
