"""Private local-goal measurement on sealed, exactly replayed C06 history."""

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json
from skillhorizon.dataflow.claims import identity, validate_claims
from skillhorizon.util import canonical_json, load_jsonl


PROTOCOL = "skill_declaration_v1"
REVISION = "c06_local_goals_v1"
SCOPE = "private_c06_local_goals_not_business_reward"


class ClaimReplay:
    def __init__(self, claims, evidence):
        self.evidence = evidence
        self.by_cut = defaultdict(list)
        for claim in claims:
            self.by_cut[claim["last_resolved_business_dispatch"]].append(deepcopy(claim))
        self.results, self.visited = [], 0

    def __call__(self, cut, runtime):
        if (type(cut) is not int or cut != self.visited
                or type(self.evidence.dispatches) is not int or self.evidence.dispatches != cut):
            raise ValueError("Claims require consecutive cuts after creation evidence is recorded")
        self.visited += 1
        if cut not in self.by_cut:
            return
        from .claim_goals import C06Goals
        goals = C06Goals(runtime, self.evidence)
        for claim in self.by_cut[cut]:
            result = goals.assess(deepcopy(claim))
            if (not isinstance(result, dict) or identity(result) != identity(claim)
                    or type(result.get("event_index")) is not int
                    or type(result.get("last_resolved_business_dispatch")) is not int
                    or result.get("goal_verdict") not in {"supported", "contradicted", "unknown"}
                    or not isinstance(result.get("clauses"), list)
                    or not isinstance(result.get("reason"), str)
                    or result.get("scope") != claim.get("scope", [])
                    or result.get("process_adherence") != "unobserved"):
                raise ValueError("C06 local-goal predicate returned an invalid measurement")
            clauses = result["clauses"]
            if (any(not isinstance(item, dict) or not isinstance(item.get("rule"), str)
                    or not item["rule"] or "passed" not in item
                    or (item["passed"] is not None and type(item["passed"]) is not bool) for item in clauses)
                    or (result["goal_verdict"] == "supported" and
                        (not clauses or any(item["passed"] is not True for item in clauses)))
                    or (result["goal_verdict"] == "contradicted" and
                        not any(item["passed"] is False for item in clauses))):
                raise ValueError("C06 local-goal clauses disagree with the measurement")
            self.results.append(deepcopy(result))


def evaluate_claims(task_root, state_root):
    """One post-seal replay, with no actor tools or feedback to the business run."""
    from skillhorizon.experiment.tasks import load_task
    from integrations.internal_mcp.framework import evaluate
    from .claim_sources import CreationEvidence

    task_root, state_root = Path(task_root), Path(state_root)
    invalid = {"valid": False, "claims": [], "revision": REVISION, "scope": SCOPE}
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
                or document.get("schema") != PROTOCOL
                or canonical_json(final["declarations"]) != canonical_json(document)):
            return {**invalid, "reason": "INVALID_FINAL_DECLARATION_EVIDENCE"}
        task = load_task(task_root)
        if (task.get("format") != "skillhorizon.internal_mcp_task.v1"
                or task.get("config", {}).get("environment_id") != "C06"
                or task.get("environment_format") != "skillhorizon.c06.reach_network.v2"
                or task.get("skill_protocol") != PROTOCOL
                or type(task.get("checkpoint_action_count")) is not int
                or task["checkpoint_action_count"] != 0):
            return {**invalid, "reason": "UNSUPPORTED_TASK"}
        count = len(load_jsonl(state_root / "runtime/model_skill_actions.jsonl"))
        if type(gateway.get("dispatched")) is not int or gateway["dispatched"] != count:
            return {**invalid, "reason": "GATEWAY_DISPATCH_COUNT_MISMATCH"}
        claims = validate_claims(document, task, count)
        evidence = CreationEvidence()
        measurement = ClaimReplay(claims, evidence)
        replay = evaluate(task, task_root / "checkpoint", state_root / "runtime",
                          claim_observer=measurement, observer=evidence.observe)
        if replay.get("valid") is not True:
            return {**invalid, "reason": "INVALID_BUSINESS_REPLAY"}
        if (type(replay.get("executed_tool_calls")) is not int or replay["executed_tool_calls"] != count
                or type(evidence.dispatches) is not int or evidence.dispatches != count
                or measurement.visited != count + 1
                or Counter(map(identity, claims)) != Counter(map(identity, measurement.results))):
            return {**invalid, "reason": "INCOMPLETE_CLAIM_COVERAGE"}
        totals = Counter(item["goal_verdict"] for item in measurement.results)
        return {"valid": True, "revision": REVISION, "claims": measurement.results,
                "completed_count": len(claims),
                "counts": {key: totals[key] for key in ("supported", "contradicted", "unknown")},
                "assessable_fraction": (totals["supported"] + totals["contradicted"]) / len(claims) if claims else None,
                "visited_cuts": measurement.visited, "replay_dispatches": count,
                "additional_replay_passes": 1, "scope": SCOPE, "process_adherence": "unobserved",
                "limitations": ["Local results do not establish skill selection or process adherence",
                    "Understanding and unresolved targets remain unknown, not omitted",
                    "Only the first completed declaration at its actual business cut is assessed",
                    "A supported local goal does not imply full business-task completion"]}
    except Exception as error:
        # A broken private measurement must not label a skill as unsuccessful.
        return {**invalid, "reason": "CLAIM_EVALUATION_FAILED", "failure_type": type(error).__name__}
