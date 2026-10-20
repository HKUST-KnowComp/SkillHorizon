"""Public task disclosure for new runs; business tools retain their semantics."""
from copy import deepcopy
import json
from pathlib import Path

MODES = {"initial_brief", "tool_discovery"}
ENVIRONMENTS = {"FinanceOps", "FieldOps", "SocialOps", "LogisticsOps",
                "ResearchOps", "DocOps", "FactoryOps"}
TOOL = "read_task_brief"
RETENTION = "ordinary_history_v1"
SCHEMA = {
    "type": "function", "name": TOOL,
    "description": "Read the initial public task brief and skill catalog. This is a task-start snapshot, not current business progress. Use the environment's business queries for current state.",
    "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
}


def _read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def prepare_task_entry(name, task, mode):
    """Extract only the release's known public entry, never its private state."""
    if name not in ENVIRONMENTS or mode not in MODES:
        raise ValueError("Unknown environment or task entry mode")
    task = Path(task)
    if name in {"FinanceOps", "FieldOps"}:
        path = task / "task.json"
        if not path.is_file():
            path = task / "environment/business/task/task.json"
        spec = _read(path)
        messages = spec.get("messages", [])
        if spec.get("checkpoint_action_count", 0) != 0:
            raise ValueError("Task entry conversion requires a zero-prefix task")
        if (name == "FinanceOps" and spec.get("format") == "skillhorizon.dataflow_v6.tasks.v1"
                and len(messages) == 1 and messages[0].get("role") == "user"):
            brief = json.loads(messages[0]["content"])
            catalog = brief.get("skill_catalog")
        elif (name == "FieldOps" and spec.get("format") == "skillhorizon.operations.v1"
              and len(messages) == 2 and all(m.get("role") == "user" for m in messages)
              and messages[0].get("content", "").startswith("ACTIVE PROJECT. ")
              and messages[1].get("content") ==
                  f"Complete project {spec.get('task_id')} using the available tools."):
            brief = json.loads(messages[0]["content"][len("ACTIVE PROJECT. "):])
            catalog = brief.get("skill_catalog")
        else:
            raise ValueError("Unsupported public task messages; explicit entry mapping required")
    else:
        brief = _read(task / "public/task.json")
        catalog = brief.get("skills")
    if not isinstance(brief, dict) or not isinstance(catalog, list) or not catalog:
        raise ValueError("Public task brief must include its complete skill catalog")
    instructions = (
        f"Complete the {name} business task using public tools. "
        "The initial public task brief describes the goal, requirements and available skills. "
        "Read relevant skill documents with read_skill_file. Begin each skill execution with "
        "begin_skill and close it with finish_skill (completed/blocked/abandoned). "
        "Calls may be associated using optional skill_run_id envelope metadata, not business arguments. "
        "Public discovery queries may be unassigned. "
        "Declarations record your decisions, not business authorization or certification. "
        "Judge completion against the whole task. "
        "read_task_brief returns a task-start snapshot, not current progress; "
        "use the environment's existing business queries for current state. "
    )
    instructions += ("The task brief is supplied below and can be reread with read_task_brief."
                     if mode == "initial_brief" else
                     "Obtain the task brief by calling read_task_brief with empty arguments.")
    return validate_task_entry(dict(version=1, environment=name, mode=mode,
        retention=RETENTION, instructions=instructions, brief_snapshot=brief))


def validate_task_entry(entry):
    if entry is None:
        return None
    if (not isinstance(entry, dict) or set(entry) !=
            {"version", "environment", "mode", "retention", "instructions", "brief_snapshot"}
            or entry["version"] != 1 or entry["environment"] not in ENVIRONMENTS
            or entry["mode"] not in MODES or entry["retention"] != RETENTION
            or not isinstance(entry["instructions"], str) or not entry["instructions"].strip()
            or not isinstance(entry["brief_snapshot"], dict)):
        raise ValueError("Invalid task entry contract")
    return json.loads(json.dumps(entry, ensure_ascii=False, allow_nan=False))


def initial_history(entry):
    if entry["mode"] == "tool_discovery":
        return []
    return [{"role": "user", "content": "Initial public task brief (task-start snapshot):\n" +
             json.dumps(entry["brief_snapshot"], ensure_ascii=False, sort_keys=True)}]


class TaskEntryClient:
    """Add one read-only public tool without changing domain dispatch counters."""
    def __init__(self, client, entry):
        self.client = client
        self.entry = validate_task_entry(entry)

    def tools(self):
        listing = deepcopy(self.client.tools())
        if any(t["name"] == TOOL for t in listing["tools"]):
            raise ValueError("Task brief tool collides with a domain tool")
        listing["tools"].append(deepcopy(SCHEMA))
        return listing

    def call(self, name, arguments, **metadata):
        if name != TOOL:
            return self.client.call(name, arguments, **metadata)
        if arguments != {}:
            raise ValueError("read_task_brief requires empty arguments")
        return {"ok": True, "status": "success", "data": deepcopy(self.entry["brief_snapshot"])}
