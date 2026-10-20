"""Private C25 local-goal checks at the first completed declaration's business cut."""

from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
from jsonschema import ValidationError

from integrations.internal_mcp.io import read_json
from skillhorizon.util import load_jsonl
from .kernel import decode_contract, BusinessError
from .portfolio_evaluation import expected_capacity


def same(left, right):
    return json.dumps(left, sort_keys=True, allow_nan=False) == json.dumps(right, sort_keys=True, allow_nan=False)


class PageGoals:
    """Pure current-cut predicates; no runtime methods, tool calls or future state."""

    def __init__(self, world):
        self.world, self.state = world, world["planning"]
        self.pages = {p["page_id"]: p for p in world["tables"]["page"]}
        self.blocks = {b["block_id"]: b for b in world["tables"]["page_block"]}
        self.reviews = {r["review_id"]: r for r in world["tables"]["review_request"]}
        self.decisions = {d["decision_id"]: d for d in world["tables"]["decision_record"]}
        self.users = {u["user_id"]: u for u in world["tables"]["user"]}
        self.requirements = {pid: json.loads(self.blocks[p["requirements_block_id"]]["content"])
                             for pid, p in self.pages.items()}
        self.expected, self.review_cache, self.publication_cache = {}, {}, {}

    def snapshot(self, page):
        return [{k: b[k] for k in ("block_id", "position", "block_type", "content", "checked")}
                for b in sorted(self.blocks.values(), key=lambda b: b["position"]) if b["page_id"] == page]

    def ideal(self, page, trail=()):
        if page in trail:
            raise ValueError("Cyclic contract requirements")
        if page not in self.expected:
            requirement = self.requirements[page]
            parents = [self.ideal(p, (*trail, page)) for p in requirement["parent_page_ids"]]
            fields = deepcopy(requirement["local_fields"])
            for parent in parents:
                for name, definition in parent["fields"].items():
                    if name in fields and fields[name] != definition:
                        raise ValueError("Incompatible declared fields")
                    fields[name] = definition
            result = {"fields": fields, "limits": {key: min([value] + [p["limits"][key] for p in parents])
                      for key, value in requirement["local_limits"].items()}}
            result["limits"]["batch_size"], result["capacity"] = expected_capacity(requirement, parents)
            self.expected[page] = result
        return self.expected[page]

    def contract(self, page, content, trail=()):
        candidate = decode_contract(content, capacity_network=True, portfolio_network=True)
        errors = []
        ideal = self.ideal(page)
        for field in ("fields", "limits", "capacity"):
            if not same(candidate[field], ideal[field]):
                errors.append("CONTRACT_" + field.upper())
        imports = candidate["imports"]
        if {p["page_id"] for p in imports} != set(self.requirements[page]["parent_page_ids"]):
            errors.append("PARENT_SET")
        for parent in imports:
            publication = self.state["publications"].get(parent["publication_id"])
            if (not publication or publication["page_id"] != parent["page_id"]
                    or publication["page_version"] != parent["page_version"]
                    or self.publication(parent["publication_id"], (*trail, page))):
                errors.append("CURRENT_PARENT_PUBLICATION")
        return errors

    def review(self, page, trail=()):
        if page in trail:
            return ["CYCLIC_PUBLICATION"]
        if page in self.review_cache:
            return self.review_cache[page]
        p = self.pages[page]
        errors = self.contract(page, self.blocks[p["contract_block_id"]]["content"], trail)
        review = self.reviews.get(p.get("approved_review_id"))
        if not review or p["lifecycle_status"] != "approved":
            return errors + ["REVIEW_NOT_ACCEPTED"]
        required = self.requirements[page]["reviewer_user_ids"]
        votes = review["reviewer_decisions"]
        if (review["page_id"] != page or review["page_version"] != p["version"]
                or review["content_snapshot"] != self.snapshot(page) or review["status"] != "approved"
                or review["required_signoff_count"] != len(required)
                or review["completed_signoff_count"] != len(required)
                or len(votes) != len(required) or {v["reviewer_user_id"] for v in votes} != set(required)
                or set(review.get("signed_reviewer_user_ids", [])) != set(required)):
            errors.append("CURRENT_REVIEW_AND_QUORUM")
        for vote in votes:
            role = self.users[vote["reviewer_user_id"]]["review_role"]
            needed = ({"CURRENT_PARENT_BINDINGS", "FIELD_UNION", "BATCH_SIZE", "CAPACITY_ALLOCATION"}
                      if role == "engineering" else {"RETENTION_DAYS"})
            signoffs = [e for e in self.world["tables"]["activity_event"]
                        if e.get("entity_id") == review["review_id"] and e.get("event_type") == "review.signoff_recorded"
                        and e.get("actor_user_id") == vote["reviewer_user_id"]]
            if (vote["role"] != role or vote["decision"] != "approve" or vote["page_version"] != p["version"]
                    or {c["rule"] for c in vote["checks"]} != needed
                    or not all(c["passed"] is True for c in vote["checks"])
                    or len(signoffs) != 1 or signoffs[0]["created_at"] != vote["created_at"]):
                errors.append("REVIEWER_EVIDENCE")
        if any(t["page_id"] == page and t["status"] in {"open", "reopened"} for t in self.world["tables"]["comment_thread"]):
            errors.append("OPEN_OBJECTIONS")
        ids = review["decision_ids"]
        if not ids or len(ids) != len(set(ids)):
            errors.append("REVIEW_DECISION_SET")
        for identifier in ids:
            decision = self.decisions[identifier]
            if (decision["page_id"] != page or decision["status"] != "accepted"
                    or decision.get("accepted_review_id") != review["review_id"]
                    or decision.get("accepted_page_version") != p["version"]):
                errors.append("ACCEPTED_DECISION_LINEAGE")
            if decision.get("supersedes_decision_id"):
                old = self.decisions[decision["supersedes_decision_id"]]
                if old["page_id"] != page or old["status"] != "superseded" or old.get("superseded_by") != identifier:
                    errors.append("DECISION_SUPERSESSION")
        self.review_cache[page] = errors
        return errors

    def publication(self, identifier, trail=()):
        if identifier in self.publication_cache:
            return self.publication_cache[identifier]
        publication = self.state["publications"][identifier]
        page = publication["page_id"]
        errors = list(self.review(page, trail))
        p = self.pages[page]
        review = self.reviews.get(p.get("approved_review_id"))
        if (publication["page_version"] != p["version"] or not review
                or publication["review_id"] != review["review_id"]
                or publication["blocks"] != self.snapshot(page)
                or not same(publication["contract"], json.loads(self.blocks[p["contract_block_id"]]["content"]))
                or set(publication["decision_ids"]) != set(review["decision_ids"])):
            errors.append("CURRENT_PUBLICATION_BINDING")
        rendered = "# " + p["title"] + "\n\n" + "\n\n".join(
            self.decisions[d]["decision_title"] + ": " + self.decisions[d]["rationale"] for d in publication["decision_ids"])
        if rendered != publication["rendered_markdown"]:
            errors.append("PUBLICATION_CONTENT")
        self.publication_cache[identifier] = errors
        return errors

    def assess(self, claim):
        result = {"run_id": claim["run_id"], "skill_id": claim["skill_id"],
                  "last_resolved_business_dispatch": claim["last_resolved_business_dispatch"],
                  "event_index": claim["event_index"], "goal_verdict": "unknown", "clauses": [],
                  "process_adherence": "unobserved", "scope": claim.get("scope", [])}
        scope = claim.get("scope", [])
        if len(scope) != 1 or scope[0].get("kind") != "page" or scope[0].get("id") not in self.pages:
            return {**result, "reason": "UNRESOLVED_SCOPE"}
        page, skill = scope[0]["id"], claim["skill_id"]
        outputs = claim.get("outputs", [])
        try:
            if skill == "reconcile-interface-contracts":
                candidates = [r for r in outputs if r["kind"] == "page_block"]
                if (len(candidates) != 1 or candidates[0]["id"] not in self.blocks
                        or candidates[0]["id"] != self.pages[page]["contract_block_id"]):
                    return {**result, "reason": "PROPOSED_CONTRACT_NOT_LOCATED"}
                errors = self.contract(page, self.blocks[candidates[0]["id"]]["content"])
                checked = "proposed_contract_and_current_sources"
            elif skill == "coordinate-document-review":
                errors = self.review(page)
                references = [r for r in outputs if r["kind"] == "review_request"]
                if references and (len(references) != 1 or references[0]["id"] != self.pages[page].get("approved_review_id")):
                    errors = errors + ["CLAIMED_REVIEW_NOT_CURRENT_ACCEPTED_REVIEW"]
                checked = "accepted_current_review_and_decisions"
            elif skill == "publish-reviewed-decision":
                candidates = [p for p in self.state["publications"].values() if p["page_id"] == page]
                references = [r for r in outputs if r["kind"] == "publication"]
                if references:
                    if len(references) != 1:
                        return {**result, "reason": "AMBIGUOUS_OUTPUT_PUBLICATION"}
                    candidates = [p for p in candidates if p["publication_id"] == references[0]["id"]]
                errors = ["CURRENT_PUBLICATION_MISSING"]
                for candidate in candidates:
                    candidate_errors = self.publication(candidate["publication_id"])
                    if not candidate_errors:
                        errors = []
                        break
                checked = "current_reviewed_publication"
            else:
                return {**result, "reason": "SKILL_GOAL_NOT_IMPLEMENTED"}
        except (KeyError, TypeError, ValueError, BusinessError, ValidationError):
            # Missing evidence for a resolved public goal is not an unresolved scope.
            errors, checked = ["MISSING_OR_MALFORMED_GOAL_EVIDENCE"], "goal_evidence"
        return {**result, "goal_verdict": "contradicted" if errors else "supported",
                "clauses": [{"clause": checked, "satisfied": not errors}],
                "reason": ";".join(sorted(set(errors))) if errors else "LOCAL_GOAL_SATISFIED"}


class ClaimReplay:
    def __init__(self, claims):
        self.by_cut = defaultdict(list)
        for claim in claims:
            self.by_cut[claim["last_resolved_business_dispatch"]].append(claim)
        self.results, self.visited = [], 0

    def __call__(self, cut, world):
        if type(cut) is not int or cut != self.visited:
            raise ValueError("Claim replay must visit each business cut exactly once, starting at zero")
        self.visited += 1
        if cut in self.by_cut:
            goals = PageGoals(world)
            self.results.extend(goals.assess(claim) for claim in self.by_cut[cut])


def evaluate_claims(task_root, state_root):
    """One extra replay after seal; never publishes labels from invalid final records."""
    from skillhorizon.experiment.tasks import load_task
    from integrations.internal_mcp.framework import evaluate
    task_root, state_root = Path(task_root), Path(state_root)
    invalid = {"valid": False, "claims": [], "scope": "private_c25_local_goals_not_business_reward"}
    try:
        final = read_json(state_root / "evaluation.json")
        document = read_json(state_root / "declaration_claims.json")
        gateway = final.get("gateway", {})
        if (gateway.get("sealed") is not True or gateway.get("execution_unknown") is not False
                or "inflight" not in gateway or gateway["inflight"] is not None):
            return {**invalid, "reason": "UNSEALED_OR_UNCERTAIN_BUSINESS"}
        if (final.get("business", {}).get("valid") is not True
                or final.get("declarations", {}).get("valid") is not True
                or document.get("valid") is not True or document.get("claims_file_valid") is False
                or final["declarations"] != document or document.get("schema") != "skill_declaration_v1"):
            return {**invalid, "reason": "INVALID_FINAL_DECLARATION_EVIDENCE"}
        task = load_task(task_root)
        if task["config"]["environment_id"] != "C25" or task["skill_protocol"] != "skill_declaration_v1":
            return {**invalid, "reason": "UNSUPPORTED_TASK"}
        count = len(load_jsonl(state_root / "runtime/model_skill_actions.jsonl")) - task["checkpoint_action_count"]
        claims = document["claims"]
        if (document["business_dispatches"] != count or len({c["run_id"] for c in claims}) != len(claims)
                or any(type(c["last_resolved_business_dispatch"]) is not int
                       or not 0 <= c["last_resolved_business_dispatch"] <= count for c in claims)):
            return {**invalid, "reason": "INVALID_CLAIM_CUT"}
        observer = ClaimReplay(claims)
        replay = evaluate(task, task_root / "checkpoint", state_root / "runtime", claim_observer=observer)
        if not replay["valid"]:
            return {**invalid, "reason": "INVALID_BUSINESS_REPLAY"}
        def identity(item):
            return item["run_id"], item["event_index"], item["last_resolved_business_dispatch"]
        if (observer.visited != count + 1
                or Counter(map(identity, claims)) != Counter(map(identity, observer.results))):
            return {**invalid, "reason": "INCOMPLETE_CLAIM_COVERAGE"}
        totals = Counter(r["goal_verdict"] for r in observer.results)
        return {"valid": True, "claims": observer.results, "completed_count": len(claims),
                "counts": {key: totals[key] for key in ("supported", "contradicted", "unknown")},
                "assessable_fraction": (totals["supported"] + totals["contradicted"]) / len(claims) if claims else None,
                "replay_dispatches": count, "additional_replay_passes": 1, "visited_cuts": observer.visited,
                "scope": invalid["scope"], "process_adherence": "unobserved",
                "limitations": ["Only single-page scopes and three C25 goal predicates are supported",
                    "Reconciliation proposals require an explicit existing contract page_block output",
                    "Unrecorded workspace proposals and ambiguous script outputs remain unknown"]}
    except (OSError, KeyError, TypeError, ValueError) as error:
        return {**invalid, "reason": "CLAIM_EVALUATION_FAILED", "failure_type": type(error).__name__}
