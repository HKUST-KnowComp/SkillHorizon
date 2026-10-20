"""Read-only local-goal measurements from sealed declarations and native replay."""

from collections import Counter, defaultdict
from copy import deepcopy
from pathlib import Path

from ..experiment.environment_score import evaluate_environment
from ..experiment.tasks import load_task
from ..util import canonical_json, load_json, load_jsonl


PROTOCOL = "skill_declaration_v1"
REVISION = "dataops_local_goals_v1"
SCOPE = "private_dataops_local_goals_not_business_reward"


def identity(claim):
    return (claim["run_id"], claim["skill_id"], claim["event_index"],
            claim["last_resolved_business_dispatch"])


def validate_claims(document, task, count):
    if (type(count) is not int or count < 0 or type(document.get("business_dispatches")) is not int
            or document["business_dispatches"] != count):
        raise ValueError("Declaration dispatch count differs from the native log")
    claims = document["claims"]
    if not isinstance(claims, list):
        raise ValueError("Claims must be a list")
    runs, previous_event, previous_cut = set(), 0, 0
    for claim in claims:
        if (not isinstance(claim, dict) or not isinstance(claim.get("run_id"), str)
                or not claim["run_id"] or claim["run_id"] in runs
                or claim.get("skill_id") not in task["skill_ids"]):
            raise ValueError("Invalid completed declaration identity")
        cut, event = claim.get("last_resolved_business_dispatch"), claim.get("event_index")
        if (type(cut) is not int or not previous_cut <= cut <= count
                or type(event) is not int or event <= previous_event):
            raise ValueError("Invalid first-completion event or business cut")
        for field in ("scope", "outputs"):
            refs = claim.get(field, [])
            if (not isinstance(refs, list) or len(refs) > 32 or any(
                    not isinstance(ref, dict) or set(ref) != {"kind", "id"}
                    or any(not isinstance(ref[key], str) or not ref[key] for key in ("kind", "id"))
                    for ref in refs)):
                raise ValueError("Invalid public object references")
        if "note" in claim and not isinstance(claim["note"], str):
            raise ValueError("Invalid declaration note")
        runs.add(claim["run_id"])
        previous_event, previous_cut = event, cut
    return claims


class ClaimReplay:
    def __init__(self, claims):
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
        from .claim_goals import DataflowGoals
        goals = DataflowGoals(runtime)
        for claim in self.by_cut[cut]:
            result = goals.assess(claim)
            if (identity(result) != identity(claim)
                    or result.get("goal_verdict") not in {"supported", "contradicted", "unknown"}
                    or not isinstance(result.get("clauses"), list)
                    or not isinstance(result.get("reason"), str)
                    or result.get("process_adherence") != "unobserved"):
                raise ValueError("Local-goal predicate returned an invalid measurement")
            self.results.append(deepcopy(result))


def evaluate_claims(task_root, state_root):
    """Return measurements only after all recorded business effects replay exactly."""
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
        if (task["format"] != "skillhorizon.dataflow.v2" or task.get("skill_protocol") != PROTOCOL
                or task["config"].get("business_program") != "credit_sales_v2"):
            return {**invalid, "reason": "UNSUPPORTED_TASK"}
        count = len(load_jsonl(state_root / "runtime/model_skill_actions.jsonl")) - task["checkpoint_action_count"]
        claims = validate_claims(document, task, count)
        observer = ClaimReplay(claims)
        replay = evaluate_environment(task, task_root / "checkpoint", state_root / "runtime", claim_observer=observer)
        if replay.get("valid") is not True:
            return {**invalid, "reason": "INVALID_BUSINESS_REPLAY"}
        if (observer.visited != count + 1
                or Counter(map(identity, claims)) != Counter(map(identity, observer.results))):
            return {**invalid, "reason": "INCOMPLETE_CLAIM_COVERAGE"}
        totals = Counter(result["goal_verdict"] for result in observer.results)
        return {"valid": True, "revision": REVISION, "claims": observer.results, "completed_count": len(claims),
                "counts": {key: totals[key] for key in ("supported", "contradicted", "unknown")},
                "assessable_fraction": (totals["supported"] + totals["contradicted"]) / len(claims) if claims else None,
                "replay_dispatches": count, "additional_replay_passes": 1, "visited_cuts": observer.visited,
                "scope": SCOPE, "process_adherence": "unobserved",
                "limitations": ["Measurements do not establish model skill selection or process adherence",
                    "Missing or ambiguous local targets remain unknown",
                    "Each claim resolves one source, regional-period, period or release target; multi-target scopes remain unknown",
                    "Demand rollup skills are not applicable to credit_sales_v2",
                    "A supported local goal is not independent business-task completion"]}
    except Exception as error:
        # Measurement faults must not become skill failures or alter business reward.
        return {**invalid, "reason": "CLAIM_EVALUATION_FAILED", "failure_type": type(error).__name__}
