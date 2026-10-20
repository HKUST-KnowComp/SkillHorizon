"""Private first-completion goals; one exact replay, no actor-visible feedback."""

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from integrations.internal_mcp.io import read_json
from skillhorizon.dataflow.claims import identity, validate_claims
from skillhorizon.util import canonical_json, load_jsonl
from .claim_goals import C09Goals
from .declaration_runtime import PROTOCOL
from .freight_build import FORMAT


REVISION = "c09_current_local_goals_v1"
SCOPE = "private_c09_local_goals_not_business_reward"


class ClaimReplay:
    def __init__(self, claims, initial):
        self.initial, self.by_cut = initial, defaultdict(list)
        for claim in claims:
            self.by_cut[claim["last_resolved_business_dispatch"]].append(deepcopy(claim))
        self.results, self.visited = [], 0

    def __call__(self, cut, runtime):
        if type(cut) is not int or cut != self.visited:
            raise ValueError("Freight claims require every consecutive business dispatch cut")
        self.visited += 1
        if cut not in self.by_cut:
            return
        goals = C09Goals(runtime, self.initial)
        for claim in self.by_cut[cut]:
            result = goals.assess(deepcopy(claim))
            if (not isinstance(result, dict) or identity(result) != identity(claim)
                    or type(result.get("event_index")) is not int
                    or type(result.get("last_resolved_business_dispatch")) is not int
                    or result.get("goal_verdict") not in {"supported", "contradicted", "unknown"}
                    or result.get("scope", []) != claim.get("scope", [])
                    or result.get("outputs", []) != claim.get("outputs", [])
                    or result.get("process_adherence") != "unobserved"
                    or not isinstance(result.get("reason"), str)
                    or not isinstance(result.get("clauses"), list)):
                raise ValueError("Invalid Freight local-goal result")
            clauses = result["clauses"]
            if (any(not isinstance(c, dict) or not isinstance(c.get("rule"), str) or not c["rule"]
                    or "passed" not in c or (c["passed"] is not None and type(c["passed"]) is not bool)
                    for c in clauses)
                    or (result["goal_verdict"] == "supported" and
                        (not clauses or any(c["passed"] is not True for c in clauses)))
                    or (result["goal_verdict"] == "contradicted" and not any(c["passed"] is False for c in clauses))):
                raise ValueError("Freight clauses disagree with their verdict")
            self.results.append(deepcopy(result))


def evaluate_claims(task_root, state_root):
    from integrations.internal_mcp.framework import evaluate
    from skillhorizon.experiment.tasks import load_task

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
                or document.get("valid") is not True or document.get("claims_file_valid") is False
                or document.get("schema") != PROTOCOL
                or canonical_json(final.get("declarations")) != canonical_json(document)):
            return {**invalid, "reason": "INVALID_FINAL_DECLARATION_EVIDENCE"}
        task = load_task(task_root)
        if (task.get("format") != "skillhorizon.internal_mcp_task.v1"
                or task.get("config", {}).get("environment_id") != "C09"
                or task.get("environment_format") != FORMAT or task.get("skill_protocol") != PROTOCOL
                or type(task.get("checkpoint_action_count")) is not int or task["checkpoint_action_count"] != 0):
            return {**invalid, "reason": "UNSUPPORTED_TASK"}
        count = len(load_jsonl(state_root / "runtime/model_skill_actions.jsonl"))
        if type(gateway.get("dispatched")) is not int or gateway["dispatched"] != count:
            return {**invalid, "reason": "GATEWAY_DISPATCH_COUNT_MISMATCH"}
        claims = validate_claims(document, task, count)
        observer = ClaimReplay(claims, read_json(task_root / "checkpoint/world.json"))
        replay = evaluate(task, task_root / "checkpoint", state_root / "runtime", claim_observer=observer)
        if replay.get("valid") is not True:
            return {**invalid, "reason": "INVALID_BUSINESS_REPLAY"}
        if (type(replay.get("executed_tool_calls")) is not int or replay["executed_tool_calls"] != count
                or observer.visited != count + 1
                or Counter(map(identity, claims)) != Counter(map(identity, observer.results))):
            return {**invalid, "reason": "INCOMPLETE_CLAIM_COVERAGE"}
        counts = Counter(row["goal_verdict"] for row in observer.results)
        return {"valid": True, "claims": observer.results, "revision": REVISION, "scope": SCOPE,
                "completed_count": len(claims), "visited_cuts": observer.visited, "replay_dispatches": count,
                "additional_replay_passes": 1, "process_adherence": "unobserved",
                "counts": {name: counts[name] for name in ("supported", "contradicted", "unknown")},
                "assessable_fraction": (counts["supported"] + counts["contradicted"]) / len(claims) if claims else None,
                "limitations": ["Current local validity, not global business completion or historical release",
                    "Understanding, plan selection and process adherence are unobserved",
                    "First completed cut is fixed; later repair does not backdate success"]}
    except Exception as error:
        return {**invalid, "reason": "CLAIM_EVALUATION_FAILED", "failure_type": type(error).__name__}
