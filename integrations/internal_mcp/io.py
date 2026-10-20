"""Small JSON and isolated-import primitives; no domain or evaluator logic."""

import importlib
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def materialize_skills(source, target):
    """Copy immutable templates into a fresh, editable task skill library."""
    target = Path(target)
    origin, destination = Path(source).resolve(), target.resolve()
    if origin.is_relative_to(destination) or destination.is_relative_to(origin):
        raise ValueError("Skill source and task copy must be disjoint")
    shutil.copytree(source, target, symlinks=True,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    entries = [(path, path.lstat().st_mode) for path in (target, *target.rglob("*"))]
    if any(not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)) for _, mode in entries):
        raise ValueError("Skill templates must contain only directories and regular files")
    for path, mode in entries:
        path.chmod(stat.S_IMODE(mode) | stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0))


def controller_source(task):
    task = Path(task).resolve()
    source = Path(read_json(task / "controller.json")["source"])
    return source.resolve() if source.is_absolute() else (task / source).resolve()


def trace_event(name, arguments, output):
    """Detach the observed response from any later planner-side mutation."""
    return deepcopy({"name": name, "arguments": arguments, "output": output})


def local_entity_ids(world, public, primary_keys, *, json_text_fields=()):
    """Short task-local identities without changing business data or references."""
    identities = {}
    for kind, key in primary_keys.items():
        for index, row in enumerate(world["tables"][kind], 1):
            old = row[key]
            if old in identities:
                raise ValueError("Entity identities must be globally unique within the task")
            identities[old] = f"{kind.replace('_', '-')}-{index:03d}"
    if len(set(identities.values())) != len(identities):
        raise ValueError("Task-local identity collision")

    def replace(value):
        if isinstance(value, str):
            return identities.get(value, value)
        if isinstance(value, list):
            return [replace(item) for item in value]
        if isinstance(value, dict):
            return {identities.get(key, key): replace(item) for key, item in value.items()}
        return value

    for table, field in json_text_fields:
        for row in world["tables"][table]:
            try:
                decoded = json.loads(row[field])
            except (ValueError, TypeError):
                continue
            row[field] = json.dumps(replace(decoded), ensure_ascii=True, allow_nan=False)
    return replace(world), replace(public)


def write_json(path, value):
    path = Path(path)
    payload = json.dumps(value, ensure_ascii=True, allow_nan=False, indent=2) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_backend(source):
    source = Path(source).resolve()
    expected = source / "mcp_servers/server.py"
    if not expected.is_file():
        raise ValueError("Source must contain mcp_servers/server.py")
    existing = sys.modules.get("mcp_servers")
    if existing is not None:
        paths = [Path(p).resolve() for p in existing.__path__]
        if paths != [source / "mcp_servers"]:
            raise ValueError("Use a separate process for each internal MCP domain")
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))
    backend = importlib.import_module("mcp_servers.server")
    if Path(backend.__file__).resolve() != expected:
        raise ValueError("MCP import resolved outside the requested source")
    backend.load_tools()
    return backend
