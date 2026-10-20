"""Private C06 local goals at one replay cut; no business calls or state writes."""

from copy import deepcopy

from .claim_sources import REFERENCE_KINDS, TABLE_KEYS, SemanticChecks, clause, objects, verdict
from .kernel import BusinessError


INSPECT = "inspect-campaign-brief"
ADAPT = "adapt-campaign-content"
APPROVAL = "coordinate-content-approval"
RELEASE = "schedule-cross-platform-release"
ANALYSIS = "analyze-campaign-experiment"
PROPAGATE = "propagate-campaign-learning"
EXPOSURE = "reconcile-audience-exposure"
REACH = "reconcile-converted-audience"
OUTPUT_KINDS = {
    ADAPT: {"content_post", "campaign_report"},
    APPROVAL: {"content_post", "approval_request"},
    RELEASE: {"content_post", "publication"},
    ANALYSIS: {"campaign_report", "ab_test", "engagement_snapshot", "content_post"},
    PROPAGATE: {"content_post", "publication", "campaign_report"},
    EXPOSURE: {"content_post", "publication"},
    REACH: {"content_post", "publication"},
}


class C06Goals:
    """One instance per cut. Semantic caches never cross a business dispatch."""

    def __init__(self, runtime, evidence):
        self.runtime = runtime
        self.evidence = evidence
        self.semantics = SemanticChecks(runtime, evidence)

    def _references(self, refs, label, checks):
        if not isinstance(refs, list):
            raise ValueError("C06 claim references must be a validated list")
        result = []
        for ref in refs:
            if (not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                    or not isinstance(ref["kind"], str) or not isinstance(ref["id"], str)):
                raise ValueError("C06 claim reference must contain kind and id strings")
            kind, identifier = ref["kind"], ref["id"]
            if kind not in REFERENCE_KINDS:
                checks.append(clause(label + "_KIND", False))
                continue
            item = objects(self.runtime, kind).get(identifier)
            checks.append(clause(label + "_EXISTS", item is not None))
            if item is None:
                continue
            if kind in TABLE_KEYS:
                checks.append(clause(label + "_TASK_OWNERSHIP",
                                     item.get("task_id") == self.runtime.world["task_id"]))
            result.append((kind, identifier, item))
        return result

    @staticmethod
    def _single(values, checks, rule="TARGET_UNRESOLVED"):
        values = set(values)
        if len(values) != 1 or None in values:
            checks.append(clause(rule, None))
            return None
        return next(iter(values))

    def _post_target(self, name, scope, outputs, checks):
        # Context reports never imply a propagation destination. Direct scope
        # posts anchor split scopes; every output is still checked afterwards.
        direct = [identifier for kind, identifier, _ in scope if kind == "content_post"]
        products = [item["post_id"] for kind, _, item in outputs
                    if kind in {"content_post", "approval_request", "publication"}
                    and kind in OUTPUT_KINDS[name]]
        secondary = [item["post_id"] for kind, _, item in scope
                     if kind == "approval_request"]
        publications = [item["post_id"] for kind, _, item in scope if kind == "publication"]
        return self._single(direct or products or secondary or publications, checks)

    def _brief_of(self, ref):
        kind, identifier, item = ref
        if kind == "campaign_brief":
            return identifier
        if kind in {"content_post", "campaign_report", "ab_test"}:
            return item["brief_id"]
        if kind in {"publication", "approval_request", "engagement_snapshot"}:
            post = objects(self.runtime, "content_post").get(item["post_id"])
            return post["brief_id"] if post is not None else None
        return None

    def _analysis(self, scope, outputs, semantics, checks):
        anchors = [ref for ref in scope if ref[0] in {"campaign_brief", "ab_test", "campaign_report"}]
        products = [ref for ref in outputs if ref[0] in {"campaign_report", "ab_test"}]
        target = self._single([self._brief_of(ref) for ref in anchors or products or scope or outputs], checks)
        if target is None:
            return None
        for ref in scope + outputs:
            checks.append(clause("EXPERIMENT_REFERENCE_BRIEF", self._brief_of(ref) == target))

        # A named test without a report cannot be rescued by a different test.
        selected = products or [ref for ref in scope if ref[0] in {"campaign_report", "ab_test"}]
        identifiers = []
        for kind, identifier, item in selected:
            report_id = identifier if kind == "campaign_report" else item.get("report_id")
            checks.append(clause("COMPLETED_REPORT_EXISTS", report_id is not None))
            if report_id is not None:
                identifiers.append(report_id)
        if not selected:
            identifiers = [identifier for identifier, item in objects(self.runtime, "campaign_report").items()
                           if item["brief_id"] == target]
        if not identifiers:
            checks.append(clause("COMPLETED_REPORT_EXISTS", False))
            return {"kind": "campaign_brief", "id": target}
        if selected:
            for identifier in dict.fromkeys(identifiers):
                explicit = objects(self.runtime, "campaign_report").get(identifier)
                checks.append(clause("EXPLICIT_REPORT_EXISTS", explicit is not None))
                if explicit is not None:
                    checks.extend(semantics.history("campaign_report", identifier))
                    checks.extend(semantics.report_content(explicit))
        report_id = self._single(identifiers, checks, "NON_EQUIVALENT_REPORTS")
        if report_id is None:
            return {"kind": "campaign_brief", "id": target}
        report = objects(self.runtime, "campaign_report").get(report_id)
        checks.append(clause("COMPLETED_REPORT_EXISTS", report is not None))
        if report is None:
            return {"kind": "campaign_brief", "id": target}
        checks.extend(semantics.history("campaign_report", report_id))
        # Even when creation history is absent, current objective defects remain
        # counterexamples; missing historical proof alone stays unknown.
        checks.extend(semantics.report_content(report))
        for kind, identifier, item in scope:
            related = (kind == "campaign_brief" and identifier == target
                       or kind == "campaign_report" and identifier == report_id
                       or kind == "ab_test" and identifier == report["ab_test_id"]
                       or kind == "content_post" and identifier in {row["post_id"] for row in report["observations"]}
                       or kind == "engagement_snapshot" and identifier in report["snapshot_ids"]
                       or kind == "publication" and identifier in {row["publication_id"] for row in report["observations"]})
            checks.append(clause("SCOPE_EXPERIMENT_EDGE", related))
        for kind, identifier, item in outputs:
            related = (kind == "campaign_report" and identifier == report_id
                       or kind == "ab_test" and identifier == report["ab_test_id"]
                       or kind == "engagement_snapshot" and identifier in report["snapshot_ids"]
                       or kind == "content_post" and identifier == report["winning_post_id"])
            checks.append(clause("OUTPUT_EXPERIMENT_EDGE", related))
        return {"kind": "campaign_report", "id": report_id}

    def _publication(self, name, post, scope, outputs, checks):
        named = [identifier for kind, identifier, _ in outputs if kind == "publication"]
        post_output = any(kind == "content_post" for kind, _, _ in outputs)
        if not named and not post_output:
            named = [identifier for kind, identifier, item in scope
                     if kind == "publication" and item["post_id"] == post["post_id"]]
        historical = name == RELEASE or name in {PROPAGATE, EXPOSURE, REACH} and bool(named)
        if not historical:
            return "current", None
        if named:
            identifier = self._single(named, checks, "NON_EQUIVALENT_PUBLICATIONS")
        else:
            identifier = post.get("publication_id")
        item = objects(self.runtime, "publication").get(identifier)
        checks.append(clause("PUBLICATION_EXISTS", item is not None))
        if item is not None:
            checks.append(clause("PUBLICATION_TARGET", item["post_id"] == post["post_id"]
                                 and post.get("publication_id") == identifier))
        return "historical", item

    def _post_references(self, name, post, publication, scope, outputs, checks):
        content = publication["content"] if publication is not None else post
        learning = content["learning"]
        reports = learning["report_ids"] if learning is not None else []
        exposure = (publication["exposure"] if publication is not None
                    else self.runtime.state["exposure_bindings"].get(post["post_id"]))
        reach = (publication["reach"] if publication is not None
                 else self.runtime.state["reach_bindings"].get(post["post_id"]))
        for label, refs in (("SCOPE", scope), ("OUTPUT", outputs)):
            for kind, identifier, item in refs:
                related = False
                if kind == "content_post":
                    related = identifier == post["post_id"]
                elif kind == "campaign_brief":
                    related = label == "SCOPE" and identifier == content["brief_id"]
                elif kind == "campaign_report":
                    related = identifier in reports and (label == "SCOPE" or name in {ADAPT, PROPAGATE})
                elif kind == "approval_request":
                    related = item["post_id"] == post["post_id"]
                    if label == "OUTPUT":
                        related = related and name == APPROVAL and identifier == post.get("approved_request_id")
                elif kind == "publication":
                    related = item["post_id"] == post["post_id"] and identifier == post.get("publication_id")
                    if label == "SCOPE" and name in {RELEASE, EXPOSURE} and exposure is not None:
                        related = related or identifier in exposure["prior_publication_ids"]
                elif kind == "engagement_snapshot" and label == "SCOPE" and name in {RELEASE, REACH}:
                    related = reach is not None and identifier in reach["conversion_snapshot_ids"]
                checks.append(clause(label + "_TARGET_EDGE", related))

    def assess(self, claim):
        result = {key: deepcopy(claim[key]) for key in
                  ("run_id", "skill_id", "event_index", "last_resolved_business_dispatch")}
        checks = []
        result.update(scope=deepcopy(claim.get("scope", [])), clauses=checks,
                      process_adherence="unobserved",
                      process_adherence_reason="NO_TRUSTED_PER_RUN_PROCESS_EVIDENCE")
        name = claim["skill_id"]
        scope = self._references(claim.get("scope", []), "SCOPE", checks)
        outputs = self._references(claim.get("outputs", []), "OUTPUT", checks)
        if name == INSPECT:
            checks.append(clause("UNDERSTANDING_UNOBSERVED", None))
            result.update(goal_verdict="unknown", reason="UNDERSTANDING_UNOBSERVED")
            return result
        if name not in OUTPUT_KINDS:
            raise ValueError("Unsupported C06 goal skill")
        checks.extend(clause("OUTPUT_ROLE", kind in OUTPUT_KINDS[name]) for kind, _, _ in outputs)
        semantics = self.semantics
        try:
            if name == ANALYSIS:
                result["evidence_mode"] = "historical"
                target = self._analysis(scope, outputs, semantics, checks)
                if target is not None:
                    result["resolved_target"] = target
            else:
                target = self._post_target(name, scope, outputs, checks)
                if target is not None:
                    result["resolved_target"] = {"kind": "content_post", "id": target}
                    post = objects(self.runtime, "content_post").get(target)
                    checks.append(clause("TARGET_POST_EXISTS", post is not None))
                    if post is not None:
                        mode, publication = self._publication(name, post, scope, outputs, checks)
                        result["evidence_mode"] = mode
                        self._post_references(name, post, publication, scope, outputs, checks)
                        if mode == "historical":
                            if publication is not None:
                                checks.extend(semantics.history("publication", publication["publication_id"]))
                        elif name in {ADAPT, PROPAGATE}:
                            checks.extend(semantics.content(target))
                        elif name == APPROVAL:
                            checks.extend(semantics.approval(target))
                        elif name in {EXPOSURE, REACH}:
                            checks.extend(semantics.binding(target, "exposure" if name == EXPOSURE else "reach"))
        except BusinessError as error:
            checks.append(clause(str(error), False))
        result["goal_verdict"] = verdict(checks)
        failed = [item["rule"] for item in checks if item["passed"] is False]
        unknown = [item["rule"] for item in checks if item["passed"] is None]
        result["reason"] = (failed or unknown or ["LOCAL_RESULT_AND_EXPLICIT_OUTPUTS_SATISFIED"])[0]
        return result
