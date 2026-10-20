"""Public declaration protocol; no business reads, authorization or evaluation."""

from copy import deepcopy
import json
import math
from pathlib import Path

from jsonschema import Draft202012Validator


PROTOCOL = "skill_declaration_v1"
NAMES = frozenset({"begin_skill", "finish_skill"})
RETIRED = NAMES | {"read_skill_run", "select_skill_run"}
LABEL = {"type": "string", "minLength": 1, "maxLength": 64,
         "pattern": "^[A-Za-z0-9][A-Za-z0-9_.:-]*$", "not": {"pattern": "[^A-Za-z0-9_.:-]"}}
REFS = {"type": "array", "maxItems": 32, "items": {"type": "object",
    "properties": {"kind": LABEL, "id": {"type": "string", "minLength": 1, "maxLength": 256}},
    "required": ["kind", "id"], "additionalProperties": False}}
TOOLS = [
    {"type": "function", "name": "begin_skill",
     "description": "Declare use of a public skill for an optional local scope; does not bind inputs or authorize business operations.",
     "parameters": {"type": "object", "properties": {"run_id": LABEL,
        "skill_id": {"type": "string", "minLength": 1, "maxLength": 128}, "scope": REFS},
        "required": ["run_id", "skill_id"], "additionalProperties": False}},
    {"type": "function", "name": "finish_skill",
     "description": "Close a declaration. completed claims the local goal is achieved, not business certification; blocked/abandoned do not claim completion.",
     "parameters": {"type": "object", "properties": {"run_id": LABEL,
        "status": {"enum": ["completed", "blocked", "abandoned"]}, "outputs": REFS,
        "note": {"type": "string", "maxLength": 1024}},
        "required": ["run_id", "status"], "additionalProperties": False}},
]
VALIDATORS = {t["name"]: Draft202012Validator(t["parameters"]) for t in TOOLS}
MISSING = object()
MAX_EVENT_BYTES = 2 * 1024 * 1024 + 16384
MAX_JSON_DEPTH = 64


def strict_json(value):
    def pairs(items):
        result = {}
        for key, item in items:
            key.encode("utf-8")
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result

    def constant(_):
        raise ValueError("Non-finite JSON number")

    value = json.loads(value, object_pairs_hook=pairs, parse_constant=constant)
    # The C JSON decoder may accept deeper trees than Python deepcopy can handle.
    stack = [iter([value])]
    while stack:
        item = next(stack[-1], MISSING)
        if item is MISSING:
            stack.pop()
        elif isinstance(item, (dict, list)):
            if len(stack) > MAX_JSON_DEPTH:
                raise ValueError("JSON nesting exceeds transport limit")
            stack.append(iter(item.values() if isinstance(item, dict) else item))
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError("Non-finite JSON number")
        elif isinstance(item, str):
            item.encode("utf-8")
    return value


def enabled(task):
    if task.get("skill_protocol") != PROTOCOL:
        return False
    skills = task.get("skill_ids")
    if (not isinstance(skills, list) or not skills or any(not isinstance(s, str) or not s or len(s) > 128 for s in skills)
            or len(skills) != len(set(skills))):
        raise ValueError("Declaration-ready task requires its complete unique skill_ids")
    if any(t["name"] in RETIRED for t in task["tools"]):
        raise ValueError("Declaration-ready task tools must be business-only")
    return True


class DeclarationState:
    """Small protocol state reconstructed only from trusted complete events."""

    def __init__(self, skill_ids):
        self.skills = frozenset(skill_ids)
        self.runs = {}
        self.count = 0
        self.last_event = 0
        self.last_cut = 0

    def propose(self, name, arguments, event_index, business_dispatch):
        if (name not in NAMES or type(event_index) is not int or event_index <= self.last_event
                or type(business_dispatch) is not int or business_dispatch < self.last_cut):
            raise ValueError("Invalid declaration order or business cut")
        receipt = {"sequence": event_index, "result": {}}
        outcome = "rejected"
        error = None
        if not VALIDATORS[name].is_valid(arguments):
            error = "DECLARATION_SCHEMA"
        elif name == "begin_skill" and arguments["skill_id"] not in self.skills:
            error = "UNKNOWN_SKILL"
        else:
            run = self.runs.get(arguments["run_id"])
            previous = run.get(name) if run else None
            if previous is not None:
                if previous["arguments"] != arguments:
                    error = "DECLARATION_CONFLICT"
                else:
                    outcome = "repeated"
                    receipt = deepcopy(previous["receipt"])
            elif name == "finish_skill" and run is None:
                error = "UNKNOWN_RUN"
            else:
                outcome = "accepted"
                receipt["result"] = {"run_id": arguments["run_id"],
                                     "status": "open" if name == "begin_skill" else arguments["status"]}
        if error:
            receipt["result"] = {"declaration_error": error}
        return {"declaration_index": self.count + 1, "event_index": event_index,
                "last_resolved_business_dispatch": business_dispatch, "name": name,
                "arguments": deepcopy(arguments), "outcome": outcome, "receipt": receipt}

    def accept(self, event):
        expected = self.propose(event["name"], event["arguments"], event["event_index"],
                                event["last_resolved_business_dispatch"])
        if event != expected:
            raise ValueError("Declaration event differs from protocol replay")
        if event["outcome"] == "accepted":
            self.runs.setdefault(event["arguments"]["run_id"], {})[event["name"]] = deepcopy(event)
        self.count += 1
        self.last_event = event["event_index"]
        self.last_cut = event["last_resolved_business_dispatch"]

    def attribution(self, value=MISSING):
        if value is MISSING:
            return {"issue": "missing"}
        result = {"skill_run_id": deepcopy(value), "issue": None}
        if not isinstance(value, str):
            result["issue"] = "invalid_type"
        elif value not in self.runs:
            result["issue"] = "unknown"
        elif "finish_skill" in self.runs[value]:
            result["issue"] = "closed"
        return result

    def claims(self):
        claims = []
        for run_id, run in self.runs.items():
            finish = run.get("finish_skill")
            if finish is None or finish["arguments"]["status"] != "completed":
                continue
            begin = run["begin_skill"]["arguments"]
            claim = {"run_id": run_id, "skill_id": begin["skill_id"],
                     "event_index": finish["event_index"],
                     "last_resolved_business_dispatch": finish["last_resolved_business_dispatch"]}
            for field in ("scope",):
                if field in begin:
                    claim[field] = deepcopy(begin[field])
            for field in ("outputs", "note"):
                if field in finish["arguments"]:
                    claim[field] = deepcopy(finish["arguments"][field])
            claims.append(claim)
        return sorted(claims, key=lambda c: c["event_index"])


class DeclarationJournal:
    """Incremental reader; append and control commits are owned by gateway lock."""

    def __init__(self, path, skills):
        self.path = Path(path)
        self.state = DeclarationState(skills)
        self.offset = 0

    def sync(self, expected_count):
        if type(expected_count) is not int or expected_count < 0:
            raise ValueError("Invalid declaration count")
        if self.path.stat().st_size < self.offset:
            raise ValueError("Declaration journal truncated")
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            while True:
                line = stream.readline(MAX_EVENT_BYTES)
                if not line:
                    break
                if not line.endswith(b"\n"):
                    raise ValueError("Partial or oversized declaration event")
                self.state.accept(strict_json(line))
            self.offset = stream.tell()
        if self.state.count != expected_count:
            raise ValueError("Declaration journal/control count mismatch")
        return self.state
