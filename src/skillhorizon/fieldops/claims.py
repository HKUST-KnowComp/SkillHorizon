"""Read-only FieldOps local-goal measurements from sealed native replay."""

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from ..dataflow.claims import identity, validate_claims
from ..experiment.environment_score import evaluate_environment
from ..experiment.tasks import load_task
from ..util import canonical_json, load_json, load_jsonl


PROTOCOL = "skill_declaration_v1"
REVISION = "fieldops_local_goals_v1"
SCOPE = "private_fieldops_local_goals_not_business_reward"


class ClaimReplay:
    def __init__(self, claims, evidence):
        self.evidence = evidence
        self.by_cut = defaultdict(list)
        for claim in claims:
            self.by_cut[claim["last_resolved_business_dispatch"]].append(claim)
        self.results, self.visited = [], 0

    def __call__(self, cut, runtime):
        if type(cut) is not int or cut != self.visited:
            raise ValueError("Claims require consecutive native dispatch cuts starting at zero")
        self.visited += 1
        if cut not in self.by_cut:
            return
        from .claim_goals import FieldOpsGoals
        goals = FieldOpsGoals(runtime, self.evidence)
        for claim in self.by_cut[cut]:
            result = goals.assess(claim)
            if (identity(result) != identity(claim)
                    or type(result["event_index"]) is not int
                    or type(result["last_resolved_business_dispatch"]) is not int
                    or result.get("goal_verdict") not in {"supported", "contradicted", "unknown"}
                    or not isinstance(result.get("clauses"), list)
                    or not isinstance(result.get("reason"), str)
                    or result.get("process_adherence") != "unobserved"):
                raise ValueError("Local-goal predicate returned an invalid measurement")
            self.results.append(deepcopy(result))


def evaluate_claims(task_root, state_root):
    """Publish first-cut measurements only after one complete exact business replay."""
    task_root, state_root = Path(task_root), Path(state_root)
    invalid = {"valid": False, "claims": [], "scope": SCOPE, "revision": REVISION}
    try:
        final = load_json(state_root / "evaluation.json")
        document = load_json(state_root / "declaration_claims.json")
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
        if (task.get("format") != "skillhorizon.operations.v1"
                or task.get("skill_protocol") != PROTOCOL
                or task.get("execution_mode") != "procedural"
                or task["config"].get("business_program") != "pressure_branches_v3"
                or task["config"].get("family") != "staged_maintenance"):
            return {**invalid, "reason": "UNSUPPORTED_TASK"}
        if type(task.get("checkpoint_action_count")) is not int or task["checkpoint_action_count"] != 0:
            return {**invalid, "reason": "UNSUPPORTED_CHECKPOINT_PREFIX"}
        count = len(load_jsonl(state_root / "runtime/model_skill_actions.jsonl"))
        claims = validate_claims(document, task, count)
        from .claim_sources import CreationEvidence
        evidence = CreationEvidence()
        observer = ClaimReplay(claims, evidence)
        replay = evaluate_environment(task, task_root / "checkpoint", state_root / "runtime",
                                      observer=evidence, claim_observer=observer)
        if replay.get("valid") is not True:
            return {**invalid, "reason": "INVALID_BUSINESS_REPLAY"}
        if (type(replay.get("executed_tool_calls")) is not int or replay["executed_tool_calls"] != count
                or type(evidence.dispatches) is not int or evidence.dispatches != count
                or observer.visited != count + 1
                or Counter(map(identity, claims)) != Counter(map(identity, observer.results))):
            return {**invalid, "reason": "INCOMPLETE_CLAIM_COVERAGE"}
        totals = Counter(result["goal_verdict"] for result in observer.results)
        return {"valid": True, "revision": REVISION, "claims": observer.results, "completed_count": len(claims),
                "counts": {key: totals[key] for key in ("supported", "contradicted", "unknown")},
                "assessable_fraction": (totals["supported"] + totals["contradicted"]) / len(claims) if claims else None,
                "replay_dispatches": count, "additional_replay_passes": 1, "visited_cuts": observer.visited,
                "scope": SCOPE, "process_adherence": "unobserved",
                "limitations": ["Measurements do not establish model skill selection or process adherence",
                    "Historical operation completion does not imply current output usability",
                    "Missing or ambiguous local targets and insufficient creation evidence remain unknown",
                    "Only zero-prefix FieldOps pressure_branches_v3 tasks are qualified",
                    "A supported local goal is not independent business-task completion"]}
    except Exception as error:
        # Discard tentative measurements without changing the independent business score.
        return {**invalid, "reason": "CLAIM_EVALUATION_FAILED", "failure_type": type(error).__name__}
