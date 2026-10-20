"""Load current content tasks and preserve their chronological executed history."""

from pathlib import Path

from ..util import canonical_json, load_json, load_jsonl
from .project_runtime import PROJECT_FORMATS


def user_message(text):
    return {"role": "user", "content": text}


def action_messages(actions, namespace):
    messages = []
    for index, action in enumerate(actions, 1):
        call_id = f"{namespace}-{index}"
        messages.extend([
            {"type": "function_call", "call_id": call_id, "name": action["skill_id"],
             "arguments": canonical_json(action["arguments"])},
            {"type": "function_call_output", "call_id": call_id, "output": canonical_json(action["result"])},
        ])
    return messages


def validate_project_history(task, actions):
    if task["checkpoint_action_count"] != len(actions):
        raise ValueError("Checkpoint action count differs from public history")
    history = task["messages"][1:-1]
    namespace = history[0]["call_id"].rsplit("-", 1)[0] if actions else "history"
    if history != action_messages(actions, namespace):
        raise ValueError("Public history is not the chronological executed checkpoint transcript")


def load_task(root):
    from ..benchmark_spec import BENCHMARK_REVISION
    root = Path(root).resolve()
    task = load_json(root / "task.json")
    from .internal_mcp import FORMAT as INTERNAL_FORMAT, validate_task
    if task.get("format") == INTERNAL_FORMAT:
        return validate_task(task, root)
    if task.get("format") not in PROJECT_FORMATS or task.get("benchmark_revision") != BENCHMARK_REVISION:
        raise ValueError("Obsolete task: rebuild a current content workflow; historical task formats are not migrated")
    if task["format"] == "skillhorizon.operations.v1":
        from ..fieldops import REVISION, PROCURE_INTERFACE_REVISION
        checkpoint = load_json(root / "checkpoint/project.json")
        procurement = checkpoint["domain"] == "procureops"
        protocol = PROCURE_INTERFACE_REVISION if procurement else "chronological_ops_summary_query_v2"
        if (task.get("observation_protocol") != protocol or
                (procurement and checkpoint.get("public_interface_revision") != PROCURE_INTERFACE_REVISION) or
                task.get("environment_revision") != REVISION or checkpoint.get("environment_revision") != REVISION or
                task["config"].get("business_program") != checkpoint.get("business_program")):
            raise ValueError("Rebuild Operations tasks for the current public interface and business program")
    if task["format"] == "skillhorizon.dataflow.v2":
        from ..dataflow.support import environment_support
        policy = task["config"]["notice_policy"]
        profile = task["config"].get("workspace_profile")
        if profile != load_json(root / "checkpoint/project.json").get("workspace_profile"):
            raise ValueError("Workspace profile differs from the checkpoint")
        if profile == "release_maintenance_v2" and (task.get("environment_revision") != profile
                or task["execution_mode"] != "procedural" or task["config"]["family"] != "release_maintenance"):
            raise ValueError("Maintenance requires its versioned procedural family")
        if (task.get("environment_support") != environment_support(policy, profile)
                or load_json(root / "checkpoint/project.json")["notice_policy"] != policy):
            raise ValueError("Declared environment support differs from the implemented DataOps surface")
    build = load_json(root / "BUILD.json")
    if build.get("status") != "complete" or build["config"] != task["config"]:
        raise ValueError("Task build is incomplete or configuration differs from construction")
    if len(task.get("messages", [])) < 2 or not task.get("tools"):
        raise ValueError("Task has no complete public request")
    validate_project_history(task, load_jsonl(root / "checkpoint/model_skill_actions.jsonl"))
    from .project_runtime import project_runtime
    project_runtime(task, root / "checkpoint")
    return task
