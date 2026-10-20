"""Controller-only observations of construction calls, not expected profiles.

Entry wrappers own a fresh construction. Delegated calls are observed only inside
that entry's context; no global monkeypatching, tracing, signing or public fields.
"""

from contextvars import ContextVar
from copy import deepcopy
from functools import wraps
import inspect
import json
from pathlib import Path

from skillhorizon.util import load_json as read_json, write_json, sha256_file


KEY = "construction_provenance"
REVISION = "actual_construction_calls_v1"
_ACTIVE = ContextVar("benchmark_construction", default=None)


def _json_value(value):
    if isinstance(value, Path):
        return str(value.resolve())
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def bound_call(function, *args, **kwargs):
    """Snapshot at dispatch, before the callee can mutate supplied arguments."""
    bound = inspect.signature(function).bind(*args, **kwargs)
    supplied = list(bound.arguments)
    bound.apply_defaults()
    arguments = _json_value(bound.arguments)
    # Reject unsupported values instead of silently serializing repr/object state.
    arguments = json.loads(json.dumps(arguments, allow_nan=False))
    return {"callable": function.__module__ + "." + function.__qualname__,
        "supplied": supplied, "arguments": arguments}


def backend_content(root):
    """Same .py/.json backend selection as framework.import_task's copy loop."""
    root = Path(root)
    result = {}
    for directory in ("mcp_servers", "data"):
        parent = root / directory
        if not parent.is_dir() or parent.is_symlink():
            raise ValueError("Missing or symlinked backend directory")
        for path in sorted(parent.rglob("*")):
            if path.is_symlink():
                raise ValueError("Bundled backends must not contain symlinks")
            if path.is_file() and path.suffix in {".py", ".json"}:
                result[path.relative_to(root).as_posix()] = sha256_file(path)
    if "mcp_servers/server.py" not in result:
        raise ValueError("Source must contain mcp_servers/server.py")
    return result


def artifact_binding(root, *, task=None):
    """Associate key task/start facts, NOT a new full checkpoint integrity system.

    Main's replay/admission and existing export checks own complete world/history
    integrity. Accept a candidate task before main publishes task.json.
    """
    root = Path(root)
    task_name = "private_task.json" if (root / "private_task.json").is_file() else "task.json"
    task_path = root / task_name
    public_path = root / "public/task.json"
    if task is None:
        task = read_json(task_path) if task_path.is_file() else read_json(public_path)
    keys = ("task_id", "format", "environment_format", "instance_identity", "config",
        "scenario", "runtime_identity", "dependency_construction", "workload_policy",
        "checkpoint_action_count", "history_source", "execution_mode", "skill_protocol")
    world_path = root / "checkpoint/world.json"
    if not world_path.is_file():
        world_path = root / "checkpoint/project.json"
    world = read_json(world_path)
    world_keys = ("task_id", "project_id", "format", "domain", "environment", "family",
        "business_program", "business_scenario", "notice_policy")
    return {"task": {key: deepcopy(task[key]) for key in keys if key in task},
        "checkpoint": {key: deepcopy(world[key]) for key in world_keys if key in world}}


def load_provenance(root):
    path = Path(root) / "BUILD.json"
    return read_json(path).get(KEY) if path.is_file() else None


def _store(root, record):
    path = Path(root) / "BUILD.json"
    build = read_json(path) if path.is_file() else {}
    if KEY in build:
        raise ValueError("Construction provenance must not overwrite an existing record")
    write_json(path, {**build, KEY: record})


def observe_call(function, *args, **kwargs):
    """Main hook: replace delegate(...) with observe_call(delegate, ...)."""
    active = _ACTIVE.get()
    if active is None:
        return function(*args, **kwargs)
    event = bound_call(function, *args, **kwargs)
    event["phase"] = "delegated"
    source = event["arguments"].get("source")
    if source is not None and str(Path(source).resolve()) != active.get("source", {}).get("path"):
        # Same-source nested calls reuse the outer entry snapshot. Only an actual
        # source switch needs another content comparison, never another manifest.
        if active.get('source_recording') == 'path_only':
            event['source_path_differs_from_entry'] = True
        else:
            event["source_content_matches_entry"] = backend_content(source) == active.get("source", {}).get("content")
    active["calls"].append(event)
    result = function(*args, **kwargs)
    event["returned"] = True
    return result


def construction_entry(function=None, *, source_recording='content'):
    """Main hook for direct builder entry points; idempotent and signature preserving."""
    if function is None:
        return lambda fn: construction_entry(fn, source_recording=source_recording)
    if source_recording not in ('content','path_only'):
        raise ValueError('Unknown source recording mode')
    if getattr(function, "_construction_entry", False):
        return function

    @wraps(function)
    def wrapped(*args, **kwargs):
        if _ACTIVE.get() is not None:
            return observe_call(function, *args, **kwargs)
        entry = bound_call(function, *args, **kwargs)
        output = Path(entry["arguments"]["output"])
        if output.exists():
            raise ValueError("Construction provenance requires a fresh output")
        source = entry["arguments"].get("source")
        record = {"revision": REVISION, "calls": [dict(entry, phase="requested")],
            "scope": "controller_only_actual_calls_not_behavior"}
        if source_recording=='path_only':record['source_recording']='path_only'
        if source is not None:
            record["source"] = {"path": str(Path(source).resolve())}
            if source_recording=='content':record['source']['content']=backend_content(source)
        token = _ACTIVE.set(record)
        try:
            result = function(*args, **kwargs)
            record["calls"][0]["returned"] = True
            if source is not None and source_recording=='content' and backend_content(source) != record["source"]["content"]:
                raise ValueError("Construction source content changed during invocation")
            record["binding"] = artifact_binding(output)
            _store(output, record)
            return result
        finally:
            _ACTIVE.reset(token)

    wrapped._construction_entry = True
    return wrapped


def propagate_provenance(source, destination, *, operation, task=None):
    """Controller hook after import/export copy, before publishing BUILD/manifest.

    Return the metadata to merge into an existing BUILD write. Do not write the
    destination here: main owns that file and its admission/complete transition.
    Missing historical provenance remains missing.
    """
    if operation not in {"import", "export", "checkpoint"}:
        raise ValueError("Unknown construction propagation operation")
    source, destination = Path(source), Path(destination)
    record = load_provenance(source)
    if record is None:
        return {}
    if record.get("revision") != REVISION or record.get("binding") != artifact_binding(source):
        raise ValueError("Source construction binding differs")
    destination_binding = artifact_binding(destination, task=task)
    if destination_binding["task"]["task_id"] != record["binding"]["task"]["task_id"]:
        raise ValueError("Construction task identity differs")
    if operation == "export":
        if destination_binding != record["binding"]:
            raise ValueError("Export task/checkpoint identity facts differ")
    elif operation == "checkpoint":
        original, candidate = record["binding"], destination_binding
        allowed = {"checkpoint_action_count", "history_source"}
        if ({k: v for k, v in original["task"].items() if k not in allowed}
                != {k: v for k, v in candidate["task"].items() if k not in allowed}
                or original["checkpoint"] != candidate["checkpoint"]):
            raise ValueError("Checkpoint construction identity differs")
        before = original["task"].get("checkpoint_action_count")
        after = candidate["task"].get("checkpoint_action_count")
        if (type(before) is not int or type(after) is not int or not 0 <= before <= after
                or not isinstance(candidate["task"].get("history_source"), str)
                or not candidate["task"]["history_source"]):
            raise ValueError("Invalid checkpoint history identity")
    else:
        copied = destination / "checkpoint/environment"
        if read_json(copied / "public/task.json") != read_json(source / "public/task.json"):
            raise ValueError("Imported public task content differs")
        content = record.get("source", {}).get("content")
        if not content or backend_content(copied / "backend") != content:
            raise ValueError("Imported backend content differs from construction source")
    propagated = deepcopy(record)
    propagated.setdefault("propagation", []).append({"operation": operation,
        "source_task_id": record["binding"]["task"]["task_id"],
        "source_checkpoint_action_count": record["binding"]["task"].get("checkpoint_action_count")})
    propagated["binding"] = destination_binding
    return {KEY: propagated}


def provenance_evidence(root, *, expected_source=None):
    """Return safe verdicts alongside private call records for profile comparison."""
    record = load_provenance(root)
    if record is None:
        return None, []
    checks = []

    def check(name, valid):
        checks.append({"field": "provenance." + name, "status": "match" if valid else "mismatch"})

    try:
        check("revision", record.get("revision") == REVISION)
        check("binding", record.get("binding") == artifact_binding(root))
        for call in record.get("calls", [])[1:]:
            if "source_content_matches_entry" in call:
                check("delegated_source_content", call["source_content_matches_entry"] is True)
        if expected_source is not None:
            source = record.get("source", {})
            if not source.get("content"):
                checks.append({"field": "backend_source", "status": "unknown"})
            else:
                try:
                    check("source_content", backend_content(expected_source) == source["content"])
                except (OSError, ValueError):
                    checks.append({"field": "provenance.source_content", "status": "unknown"})
                backend = Path(root) / "checkpoint/environment/backend"
                if (Path(root) / "task.json").is_file() or (Path(root) / "private_task.json").is_file():
                    check("backend_copy", backend_content(backend) == source["content"])
    except (KeyError, TypeError, AttributeError, ValueError, OSError):
        check("readable_binding", False)
    return record, checks
