"""Score trusted, sealed FieldOps state without executing historical actions."""

from pathlib import Path

from ..util import load_json, load_jsonl
from .project_runtime import project_runtime
from .tasks import validate_project_history


def validate_sealed_gateway_records(state_root):
    """Retain the gateway's dispatch/result checks, without world replay."""
    root = Path(state_root)
    control = load_json(root / "control.json")
    if (control.get("sealed") is not True or control.get("execution_unknown") is not False
            or control.get("inflight") is not None):
        raise ValueError("Business state is not sealed and quiescent")
    events = load_jsonl(root / "gateway.jsonl")
    dispatched = [e for e in events if e["phase"] == "dispatch"]
    returned = [e for e in events if e["phase"] == "result"]
    if len(dispatched) != control["dispatched"] or len(returned) != len(dispatched):
        raise ValueError("Incomplete trusted gateway records")
    actions = load_jsonl(root / "runtime/model_skill_actions.jsonl")
    if len(actions) != len(dispatched):
        raise ValueError("Business actions differ from trusted gateway dispatches")
    for call, result, action in zip(dispatched, returned, actions):
        if (call["sequence"] != result["sequence"] or call["name"] != action["skill_id"]
                or call["arguments"] != action["arguments"] or result["result"] != action["result"]):
            raise ValueError("Gateway/business record mismatch")
    return events, actions


def evaluate_sealed_fieldops(task, checkpoint, runtime_root):
    """Use original business predicates; valid does not certify action replay.

    This route is only for private gateway state. It never calls runtime.call,
    rewrites the world, or accepts unknown/in-flight execution.
    """
    runtime_root = Path(runtime_root)
    if task["format"] != "skillhorizon.operations.v1":
        raise ValueError("Sealed-state route is limited to FieldOps")
    runtime = project_runtime(task, runtime_root)
    if runtime.project.get("domain") != "fieldops":
        raise ValueError("Sealed-state route is limited to FieldOps")
    _, observed = validate_sealed_gateway_records(runtime_root.parent)
    initial = load_jsonl(Path(checkpoint) / "model_skill_actions.jsonl")
    validate_project_history(task, initial)
    if observed[:len(initial)] != initial:
        raise ValueError("Executed checkpoint prefix changed")
    state = runtime.score()
    errors = list(state.get("state_errors", []))
    if type(state.get("success")) is not bool:
        raise ValueError("Invalid FieldOps business verdict")
    return {"valid": not errors, "errors": errors,
            "task_success": not errors and state["success"], "state": state,
            "executed_tool_calls": len(observed) - len(initial),
            "independent_content_verification": None,
            "independent_maintenance_verification": None,
            "verification_method": "sealed_fieldops_state_v1", "replay": "not_run",
            "scope": "Sealed private gateway records and original FieldOps business predicates; "
                     "does not certify historical action replay or provider configuration."}
