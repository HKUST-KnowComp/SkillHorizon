"""Private, read-only C06 predicates and small facts from one exact replay."""

from copy import copy, deepcopy
from datetime import timedelta
from functools import wraps
from hashlib import sha256
import json

from integrations.internal_mcp.tables import TableState
from .kernel import BusinessError, instant, rate, stamp


TABLE_KEYS = {
    "content_post": "post_id", "approval_request": "approval_request_id",
    "engagement_snapshot": "snapshot_id", "ab_test": "ab_test_id",
}
STATE_KEYS = {
    "publication": "publications", "campaign_report": "reports",
    "campaign_brief": "briefs",
}
REFERENCE_KINDS = frozenset(TABLE_KEYS) | frozenset(STATE_KEYS)


def objects(runtime, kind):
    if kind in TABLE_KEYS:
        key = TABLE_KEYS[kind]
        return {row[key]: row for row in runtime.world["tables"][kind]}
    if kind in STATE_KEYS:
        return runtime.state[STATE_KEYS[kind]]
    return {}


def fingerprint(value):
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                             allow_nan=False).encode()).hexdigest()


def clause(rule, passed):
    if passed is not None and type(passed) is not bool:
        raise TypeError("Predicate results must be bool or None")
    return {"rule": rule, "passed": passed}


def verdict(clauses):
    if any(item["passed"] is False for item in clauses):
        return "contradicted"
    if not clauses or any(item["passed"] is None for item in clauses):
        return "unknown"
    return "supported"


def business_conditions(method):
    @wraps(method)
    def checked(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except BusinessError as error:
            return [clause(str(error), False)]
    return checked


def before_view(runtime, world):
    prior = copy(runtime)
    prior.world = world
    prior.sm = TableState(world, runtime.sm.background, runtime.sm.primary_keys)
    return prior


class SemanticChecks:
    """Reuse business calculations; never invoke a business entry point."""

    def __init__(self, runtime, evidence):
        self.runtime = runtime
        self.evidence = evidence
        self._visiting = set()
        self._cache = {}

    def dependency(self, kind, identifier):
        status = verdict(self.history(kind, identifier))
        return clause("VALID_" + kind.upper() + ":" + identifier,
                      None if status == "unknown" else status == "supported")

    def history(self, kind, identifier):
        key = (kind, identifier)
        if key in self._visiting:
            return [clause("CYCLIC_BUSINESS_LINEAGE", False)]
        if key in self._cache:
            return deepcopy(self._cache[key])
        item = objects(self.runtime, kind).get(identifier)
        if item is None:
            return [clause("MISSING_" + kind.upper(), False)]
        record = self.evidence.records.get(key)
        if record is None:
            return [clause("CREATION_HISTORY_UNOBSERVED", None)]
        self._visiting.add(key)
        try:
            checks = [clause("RECORDED_CONTENT_UNCHANGED", fingerprint(item) == record["fingerprint"])]
            checks.extend(deepcopy(record["clauses"]))
            for dependency in record["dependencies"]:
                checks.append(self.dependency(*dependency))
            if kind == "publication":
                post = self.runtime.owned("content_post", item["post_id"])
                checks.append(clause("IMMUTABLE_PUBLICATION_LINK", post["status"] == "published"
                    and post.get("publication_id") == identifier
                    and item["content"] == self.runtime.snapshot(post["post_id"])
                    and item["post_version"] == post["version"]
                    and item["published_at"] == post["published_at"]))
            elif kind == "engagement_snapshot":
                checks.append(clause("CONVERSION_RECORD_UNCHANGED",
                    fingerprint(self.runtime.state["conversion_records"].get(identifier))
                    == record["conversion_fingerprint"]))
            elif kind == "campaign_report":
                checks.extend(self.report_content(item))
            self._cache[key] = checks
            return deepcopy(checks)
        finally:
            self._visiting.remove(key)

    @business_conditions
    def content(self, post_id):
        runtime = self.runtime
        post = runtime.owned("content_post", post_id)
        brief = runtime.brief(post["brief_id"])
        variant = post["role"] == "variant"
        identity = post["campaign_id"] == brief["campaign_id"] and (
            variant and type(post["variant_index"]) is int
            and 0 <= post["variant_index"] < len(brief["variant_post_ids"])
            and brief["variant_post_ids"][post["variant_index"]] == post_id
            or post["role"] == "deployment" and post["variant_index"] is None
            and brief["deployment_post_id"] == post_id)
        checks = [clause("POST_BUSINESS_IDENTITY", identity)]
        if not identity:
            return checks
        initial = self.evidence.post_identities.get(post_id)
        if initial is not None:
            checks.append(clause("INITIAL_POST_IDENTITY", self.evidence.post_identity(post) == initial))
        checks.extend(clause(item["rule"], item["passed"]) for item in runtime.checks(post_id))
        learning = post["learning"]
        if learning is not None:
            for identifier in learning["report_ids"]:
                checks.append(self.dependency("campaign_report", identifier))
        return checks

    @business_conditions
    def approval(self, post_id):
        runtime = self.runtime
        post = runtime.owned("content_post", post_id)
        review = runtime.current_approval(post_id)
        checks = self.content(post_id)
        checks.extend([
            clause("CURRENT_APPROVAL_STATUS", post["approval_status"] == "approved"),
            clause("REVIEW_IDENTITY", review["requested_by_user_id"] == runtime.public["editor_user_id"]
                   and review["assigned_reviewer_user_id"] == runtime.public["reviewer_user_id"]
                   and review["stage"] == "final_approval"),
            clause("ACTUAL_REVIEW_CHECKS", review["checks"] == runtime.checks(post_id)),
        ])
        checks.append(self.dependency("approval_request", review["approval_request_id"]))
        return checks

    @business_conditions
    def binding(self, post_id, kind):
        runtime = self.runtime
        post = runtime.owned("content_post", post_id)
        binding = runtime.state[kind + "_bindings"].get(post_id)
        checks = [clause("DUE_UNPUBLISHED_POST", post["status"] == "scheduled"
                         and post["scheduled_for"] is not None
                         and instant(post["scheduled_for"]) <= instant(runtime.now())),
                  clause("BINDING_EXISTS", binding is not None)]
        if binding is None:
            return checks
        checks.append(clause("BINDING_IDENTITY_TIME", binding["post_id"] == post_id
            and binding["post_version"] == post["version"]
            and instant(binding["as_of"]) <= instant(runtime.now())))
        if kind == "exposure":
            sources, values = runtime.accounting(post)
            checks.append(clause("EXACT_EXPOSURE", sorted(binding["prior_publication_ids"]) == sources
                                 and binding["audience_loads"] == values))
            checks.extend(self.dependency("publication", identifier) for identifier in sources)
        elif kind == "reach":
            sources, values = runtime.reach(post)
            checks.append(clause("EXACT_REACH", binding["conversion_snapshot_ids"] == sources
                                 and binding["eligible_cohorts"] == values))
            checks.extend(self.dependency("engagement_snapshot", identifier) for identifier in sources)
        else:
            raise ValueError("Unknown C06 binding kind")
        return checks

    @business_conditions
    def publication_creation(self, after, item):
        prior = self.runtime
        post_id = item["post_id"]
        post = prior.owned("content_post", post_id)
        brief = prior.brief(post["brief_id"])
        window = brief["variant_window"] if post["role"] == "variant" else brief["deployment_window"]
        checks = self.approval(post_id) + self.binding(post_id, "exposure") + self.binding(post_id, "reach")
        prior.check_slot(post, instant(prior.now()))
        checks.extend([
            clause("RELEASE_WINDOW", instant(window["start"]) <= instant(post["scheduled_for"])
                   <= instant(prior.now()) <= instant(window["end"])),
            clause("DECLARED_UPDATES", brief["wave"] <= 1
                   or len(prior.state["applied_updates"]) == len(prior.state["scheduled_updates"])),
        ])
        variant = post["variant_index"]
        if post["role"] == "deployment":
            report = prior.report(post["learning"]["report_ids"][0])
            variant = prior.owned("content_post", report["winning_post_id"])["variant_index"]
        expected = {
            "publication_id": item["publication_id"], "post_id": post_id,
            "post_version": post["version"], "content": prior.snapshot(post_id),
            "approval_request_id": post["approved_request_id"], "published_at": prior.now(),
            "scheduled_for": post["scheduled_for"],
            "asset_evidence": [prior.owned("asset", identifier) for identifier in post["asset_ids"]],
            "exposure": {**prior.state["exposure_bindings"][post_id], "as_of": prior.now(),
                         "allocated_units": brief["variant_exposure_units"][variant]},
            "reach": {**prior.state["reach_bindings"][post_id], "as_of": prior.now()},
        }
        current = after.owned("content_post", post_id)
        checks.extend([
            clause("PUBLICATION_CONTENT", item == expected),
            clause("PUBLISHED_POST_LINK", current["status"] == "published"
                   and current["approval_status"] == "approved"
                   and current.get("publication_id") == item["publication_id"]
                   and current["published_at"] == prior.now()
                   and after.snapshot(post_id) == prior.snapshot(post_id)),
        ])
        return checks

    @business_conditions
    def observation_creation(self, after, item):
        runtime = self.runtime
        post = runtime.owned("content_post", item["post_id"])
        if post["status"] != "published" or post["role"] != "variant":
            return [clause("PUBLISHED_VARIANT_REQUIRED", False)]
        end = instant(post["published_at"]) + timedelta(hours=runtime.brief(post["brief_id"])["observation_hours"])
        metrics = runtime.response_metrics(post["post_id"])
        checks = [self.dependency("publication", post["publication_id"])]
        checks.extend([
            clause("OBSERVATION_WINDOW", instant(runtime.now()) >= end
                   and item["observed_from"] == post["published_at"]
                   and item["observed_until"] == item["captured_at"] == stamp(end)
                   and item["created_at"] == runtime.now()),
            clause("OBSERVATION_LINEAGE", item["post_version"] == post["version"]
                   and item["publication_id"] == post["publication_id"]
                   and item["social_account_id"] == post["social_account_id"]
                   and item["source"] == "environment_synthetic_response"
                   and item["task_id"] == runtime.world["task_id"]),
            clause("ACTUAL_MEASUREMENT", all(item[key] == value for key, value in metrics.items())
                   and all(item[key] == 0 for key in ("likes", "comments", "shares"))
                   and item["follower_count"] == runtime.owned("social_account", post["social_account_id"])["follower_count"]),
        ])
        reach = runtime.state["publications"][post["publication_id"]]["reach"]["eligible_cohorts"]
        profile = runtime.state["cohort_response_profiles"][post["post_id"]]
        converted = {segment: [cohort for cohort in profile["conversion_order"] if cohort in group
                              and cohort in profile.get('conversion_partition', profile['conversion_order'])][:1]
                     if profile["conversion_enabled"] and item["clicks"] > 0 else []
                     for segment, group in reach.items()}
        checks.append(clause("OBSERVED_CONVERSION", after.state["conversion_records"].get(item["snapshot_id"]) == {
            "snapshot_id": item["snapshot_id"], "post_id": post["post_id"],
            "publication_id": post["publication_id"], "observed_until": item["observed_until"],
            "available_at": item["created_at"], "sequence": runtime.world["sequence"],
            "converted_cohorts": converted,
        }))
        return checks

    @business_conditions
    def report_content(self, item):
        runtime = self.runtime
        test = runtime.owned("ab_test", item["ab_test_id"])
        brief = runtime.brief(item["brief_id"])
        observations = [runtime.owned("engagement_snapshot", identifier) for identifier in item["snapshot_ids"]]
        checks = [clause("COMPLETE_EXPERIMENT", test["brief_id"] == brief["brief_id"]
            and test["campaign_id"] == brief["campaign_id"]
            and test["status"] == "completed" and test.get("report_id") == item["report_id"]
            and len(test["variant_post_ids"]) == len(brief["variant_post_ids"])
            and set(test["variant_post_ids"]) == set(brief["variant_post_ids"])
            and test["success_metric"] == item["metric"] == "clicks_per_impression"
            and test["ended_at"] == item["completed_at"]),
            clause("EXACT_OBSERVATION_SET", len(observations) == len(brief["variant_post_ids"])
                   and {row["post_id"] for row in observations} == set(brief["variant_post_ids"])
                   and len(set(item["snapshot_ids"])) == len(item["snapshot_ids"]))]
        if not observations:
            return checks + [clause("MISSING_MEASUREMENTS", False)]
        for row in observations:
            post = runtime.owned("content_post", row["post_id"])
            checks.append(self.dependency("engagement_snapshot", row["snapshot_id"]))
            checks.append(clause("REPORT_OBSERVATION_LINEAGE", row["post_version"] == post["version"]
                and row["publication_id"] == post.get("publication_id")
                and instant(row["created_at"]) <= instant(item["completed_at"]) <= instant(runtime.now())))
        winner = sorted(observations, key=lambda row: (-rate(row), row["post_id"]))[0]
        post = runtime.owned("content_post", winner["post_id"])
        checks.extend([
            clause("NORMALIZED_WINNER", item["winning_post_id"] == test["winning_post_id"] == winner["post_id"]),
            clause("REPORT_OBSERVATION_COPY", sorted(item["observations"], key=lambda row: row["snapshot_id"])
                   == sorted(observations, key=lambda row: row["snapshot_id"])
                   and item["winner_observation"] == winner),
            clause("WINNER_CONTENT", item["winner_content"] == runtime.snapshot(post["post_id"])
                   and item["winning_cta"] == brief["ctas"][post["variant_index"]]),
        ])
        return checks


class CreationEvidence:
    """Ephemeral private evidence; no IO, reducer, actor calls or world history."""

    def __init__(self):
        self.records = {}
        self.bindings = {}
        self.post_identities = {}
        self.dispatches = 0

    @staticmethod
    def post_identity(post):
        return {key: post[key] for key in ("post_id", "brief_id", "campaign_id", "social_account_id",
                                          "author_user_id", "role", "variant_index")}

    def _record(self, kind, identifier, item, checks, dependencies=(), **extra):
        key = (kind, identifier)
        if key in self.records:
            raise ValueError("A creation fact cannot be overwritten")
        self.records[key] = dict(fingerprint=fingerprint(item), business_cut=self.dispatches,
                                 clauses=deepcopy(checks), dependencies=list(dependencies), **extra)

    def observe(self, before, runtime, action, index):
        if type(index) is not int or index != self.dispatches:
            raise ValueError("Creation evidence requires contiguous zero-prefix dispatches")
        if index == 0:
            self.post_identities = {post["post_id"]: self.post_identity(post)
                                    for post in before["tables"]["content_post"]}
        self.dispatches += 1
        if action["result"]["status"] != "success":
            return
        prior = before_view(runtime, before)
        old = SemanticChecks(prior, self)
        name, args, data = action["skill_id"], action["arguments"], action["result"]["data"]
        if name == "run_content_review":
            identifier = args["approval_request_id"]
            previous = prior.owned("approval_request", identifier)
            item = runtime.owned("approval_request", identifier)
            if previous.get("environment_decision") is not None:
                return
            post_id = item["post_id"]
            checks = old.content(post_id)
            checks.extend([
                clause("APPROVAL_RESPONSE", data == item),
                clause("REVIEW_SNAPSHOT", previous["status"] != "canceled"
                       and previous["content_snapshot"] == item["content_snapshot"] == prior.snapshot(post_id)),
                clause("REVIEW_IDENTITY", item["requested_by_user_id"] == prior.public["editor_user_id"]
                       and item["assigned_reviewer_user_id"] == prior.public["reviewer_user_id"]
                       and item["stage"] == "final_approval"),
                clause("APPROVED_RESULT", item["status"] == item["environment_decision"] == "approved"
                       and item["checks"] == prior.checks(post_id)
                       and runtime.owned("content_post", post_id).get("approved_request_id") == identifier),
            ])
            dependencies = [("campaign_report", rid) for rid in (item["content_snapshot"]["learning"] or {}).get("report_ids", [])]
            self._record("approval_request", identifier, item, checks, dependencies)
        elif name == "publish_post":
            identifier = data["publication_id"]
            if identifier in prior.state["publications"]:
                return
            item = runtime.state["publications"][identifier]
            checks = old.publication_creation(runtime, item)
            checks.append(clause("PUBLICATION_RESPONSE", data == item and item["post_id"] == args["post_id"]))
            dependencies = [("approval_request", item["approval_request_id"])]
            dependencies += [("publication", pid) for pid in item["exposure"]["prior_publication_ids"]]
            dependencies += [("engagement_snapshot", sid) for sid in item["reach"]["conversion_snapshot_ids"]]
            self._record("publication", identifier, item, checks, dependencies)
        elif name == "observe_post":
            identifier = data["snapshot_id"]
            if identifier in objects(prior, "engagement_snapshot"):
                return
            item = runtime.owned("engagement_snapshot", identifier)
            checks = old.observation_creation(runtime, item)
            conversion = runtime.state["conversion_records"].get(identifier)
            checks.append(clause("OBSERVATION_RESPONSE", data == {**item, "conversion": conversion}
                                 and item["post_id"] == args["post_id"]))
            self._record("engagement_snapshot", identifier, item, checks,
                         [("publication", item["publication_id"])], conversion_fingerprint=fingerprint(conversion))
        elif name == "complete_campaign_experiment":
            identifier = data["report_id"]
            if identifier in prior.state["reports"]:
                return
            item = runtime.state["reports"][identifier]
            checks = SemanticChecks(runtime, self).report_content(item)
            checks.extend([
                clause("REPORT_RESPONSE", data == item and item["ab_test_id"] == args["ab_test_id"]
                       and item["snapshot_ids"] == sorted(args["snapshot_ids"])
                       and item["winning_post_id"] == args["winning_post_id"]),
                clause("REPORT_CREATION_TIME", item["completed_at"] == prior.now()
                       and prior.owned("ab_test", item["ab_test_id"])["status"] != "completed"),
            ])
            self._record("campaign_report", identifier, item, checks,
                         [("engagement_snapshot", sid) for sid in item["snapshot_ids"]])
        elif name in {"bind_audience_exposure", "bind_audience_reach"}:
            kind = "exposure" if name == "bind_audience_exposure" else "reach"
            identifier = args["post_id"]
            item = runtime.state[kind + "_bindings"][identifier]
            if item != prior.state[kind + "_bindings"].get(identifier):
                checks = SemanticChecks(runtime, self).binding(identifier, kind)
                checks.append(clause("BINDING_RESPONSE", data == item))
                self.bindings[(kind, identifier)] = dict(fingerprint=fingerprint(item),
                    business_cut=self.dispatches, clauses=deepcopy(checks))

    __call__ = observe
