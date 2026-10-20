"""Business/effect verification independent of model wire format or agent framework."""

from pathlib import Path
from copy import deepcopy
import shutil
from tempfile import TemporaryDirectory

from ..util import load_jsonl
from .project_runtime import project_runtime
from .tasks import validate_project_history


def evaluate_environment(task, checkpoint, runtime_root, *, observer=None, claim_observer=None):
    from .managed_session import require_world_quiescent
    require_world_quiescent(Path(runtime_root).resolve().parent)
    from .internal_mcp import FORMAT as INTERNAL_FORMAT, worker
    if task["format"] == INTERNAL_FORMAT:
        if observer is not None or claim_observer is not None:
            raise ValueError("Use internal-domain native diagnostics, not the DataOps process observer")
        from ..util import write_json
        with TemporaryDirectory(prefix="internal-score-contract-") as temporary:
            metadata = Path(temporary) / "task.json"
            write_json(metadata, task)
            return worker("evaluate", metadata, Path(checkpoint).resolve(), Path(runtime_root).resolve())
    checkpoint, runtime_root = Path(checkpoint), Path(runtime_root)
    initial = load_jsonl(checkpoint / "model_skill_actions.jsonl")
    validate_project_history(task, initial)
    observed = load_jsonl(runtime_root / "model_skill_actions.jsonl")
    errors = []
    if observed[:len(initial)] != initial:
        errors.append("Executed checkpoint prefix changed")
    with TemporaryDirectory(prefix="skillhorizon-effects-") as temp:
        replay = Path(temp) / "runtime"
        shutil.copytree(checkpoint, replay)
        runtime = project_runtime(task, replay)
        if claim_observer is not None:
            claim_observer(0, runtime)
        for index, action in enumerate(observed[len(initial):], len(initial)):
            before = deepcopy(runtime.project) if observer is not None else None
            result = runtime.call(action["skill_id"], action["arguments"])
            actual = load_jsonl(replay / "model_skill_actions.jsonl")[-1]
            if result != action["result"] or actual != action:
                errors.append(f"Action {index}: recorded effect differs from replay")
            if observer is not None:
                observer(before, runtime, action, index)
            if claim_observer is not None:
                claim_observer(index - len(initial) + 1, runtime)
        state = runtime.score()
        if runtime.snapshot() != project_runtime(task, runtime_root).snapshot():
            errors.append("Final environment differs from recorded operations")
    content = maintenance = None
    if task["format"] == "skillhorizon.dataflow.v2":
        from ..dataflow.verification import verify_release_content
        content = verify_release_content(runtime_root)
        if state["success"] and not content["passed"]:
            errors.append("Independent content verification disagrees")
        if task["config"].get("workspace_profile") == "release_maintenance_v2":
            from ..dataflow.maintenance_verification import verify_maintenance
            maintenance = verify_maintenance(runtime_root, checkpoint)
            if state["success"] and not maintenance["passed"]:
                errors.append("Independent maintenance verification disagrees")
    return {"valid": not errors, "errors": errors, "task_success": not errors and state["success"],
            "state": state, "executed_tool_calls": len(observed) - len(initial),
            "independent_content_verification": content,
            "independent_maintenance_verification": maintenance,
            "scope": "Business effects only; does not certify agent permissions, provider configuration or costs."}
