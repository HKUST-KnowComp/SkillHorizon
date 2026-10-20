"""Private exported-code ownership checks, independent of agent-written logs."""

import hashlib
from importlib.metadata import version
import json
import os
from pathlib import Path
import sys


DEPENDENCIES = ("mcp", "pydantic", "jsonschema", "PyYAML", "tiktoken", "pybtex")


def canonical_source_path(value):
    path = Path(os.path.abspath(value))
    if path != path.resolve():
        raise ValueError("Protected module path uses a symlink alias")
    return path


def attest(code_root, *, phase, required_modules=(), modules=None, task_root=None, runtime_root=None,
           entry_file=None, verify_files=True):
    code_root = Path(code_root).resolve()
    manifest_path = code_root / "CODE_ORIGINS.json"
    raw = manifest_path.read_bytes()
    manifest = json.loads(raw)
    if (type(manifest.get("version")) is not int or manifest["version"] != 1
            or phase not in {"serve", "verify", "admission"}
            or any(not isinstance(manifest.get(key), str) or not Path(manifest[key]).is_absolute()
                   for key in ("business_source_root", "adapter_source_root"))):
        raise ValueError("Invalid private source manifest")
    entries = manifest["files"]
    if not isinstance(entries, dict) or not entries:
        raise ValueError("Missing private source entries")
    for relative, entry in entries.items():
        path = code_root / relative
        if (Path(relative).is_absolute() or ".." in Path(relative).parts
                or path.is_symlink() or not path.resolve().is_relative_to(code_root)
                or entry.get("owner") not in {"business", "adapter"}
                or not path.is_file()
                or (verify_files and hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"])):
            raise ValueError("Private exported source changed or incomplete")
    task_files = manifest.get("task_python_files", {})
    for base in (Path(task_root) / "checkpoint" if task_root is not None else None, runtime_root):
        if base is None:
            continue
        base = Path(base).resolve()
        for relative, entry in task_files.items():
            path = base / relative
            if (Path(relative).is_absolute() or ".." in Path(relative).parts
                    or path.is_symlink() or not path.resolve().is_relative_to(base)
                    or not path.is_file()
                    or (verify_files and hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"])):
                raise ValueError("Frozen task Python source changed")
    loaded = {}
    for name, module in list((sys.modules if modules is None else modules).items()):
        business = name == "skillhorizon" or name.startswith("skillhorizon.")
        integration = name == "integrations" or name.startswith("integrations.")
        task_backend = name == "mcp_servers" or name.startswith("mcp_servers.")
        filename = getattr(module, "__file__", None)
        spec = getattr(module, "__spec__", None)
        if business or integration or task_backend:
            if filename is not None:
                canonical_source_path(filename)
            origin = getattr(spec, "origin", None)
            if origin is not None:
                canonical_source_path(origin)
            if origin is not None and (filename is None or Path(origin).resolve() != Path(filename).resolve()):
                raise ValueError("Module spec and file origins disagree")
            paths = list(getattr(module, "__path__", ())) + list(getattr(spec, "submodule_search_locations", ()) or ())
            for location in paths:
                resolved = canonical_source_path(location)
                if filename is not None:
                    valid = resolved == Path(filename).resolve().parent
                elif task_backend:
                    valid = any(resolved.is_relative_to(Path(base).resolve()) for base in (
                        Path(task_root) / "checkpoint" if task_root is not None else None, runtime_root) if base is not None)
                else:
                    base = code_root / "src" if business else code_root
                    valid = resolved == base.joinpath(*name.split("."))
                if not valid:
                    raise ValueError("Module package search path escaped its source")
        if filename is None:
            if task_backend:
                paths = list(getattr(module, "__path__", ()))
                bases = [Path(base).resolve() for base in (
                    Path(task_root) / "checkpoint" if task_root is not None else None, runtime_root) if base is not None]
                if not paths or not all(any(Path(p).resolve().is_relative_to(b) for b in bases) for p in paths):
                    raise ValueError("Task backend namespace escaped frozen task")
            continue
        path = Path(filename).resolve()
        task_match = None
        for base in (Path(task_root) / "checkpoint" if task_root is not None else None, runtime_root):
            if base is not None and path.is_relative_to(Path(base).resolve()):
                canonical_source_path(filename)
                relative = path.relative_to(Path(base).resolve()).as_posix()
                task_match = task_files.get(relative)
                if not task_match:
                    raise ValueError("Loaded task module is absent from frozen Python inventory")
                loaded[name] = {"path": str(path), "owner": "task_backend",
                    "original_source": task_match["source"], "sha256": task_match["sha256"]}
                break
        if task_backend and not task_match:
            raise ValueError("Task backend module escaped frozen task")
        if task_match or not (business or integration):
            continue
        if not path.is_relative_to(code_root):
            raise ValueError("Loaded business or adapter module escaped private code")
        relative = path.relative_to(code_root).as_posix()
        entry = entries.get(relative)
        owner = "adapter" if name == "integrations.harbor" or name.startswith("integrations.harbor.") else "business"
        if not entry or entry["owner"] != owner:
            raise ValueError("Loaded module has wrong source owner")
        loaded[name] = {"path": str(path), "owner": owner, "original_source": entry["source"],
                        "sha256": entry["sha256"]}
    if not set(required_modules) <= set(loaded):
        raise ValueError("Required runtime/evaluator modules were not attested")
    if entry_file is not None:
        entry = Path(entry_file).resolve()
        allowed = ({code_root / "integrations/harbor/task_admission.py"} if phase == "admission" else {
            code_root / "integrations/harbor/backend.py", code_root / "integrations/harbor/score_worker.py",
            code_root / "integrations/harbor/claims_worker.py"})
        if entry not in allowed:
            raise ValueError("Unexpected private backend entry")
        loaded["backend_entry"] = {"path": str(entry), **entries[entry.relative_to(code_root).as_posix()]}
    return {"version": 1, "phase": phase, "status": "passed", "code_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "business_source_root": manifest["business_source_root"],
        "adapter_source_root": manifest["adapter_source_root"], "modules": loaded,
        "dependency_versions": {name: version(name) for name in DEPENDENCIES},
        "dependency_scope": "actual installed versions; not a claim of original dependency identity"}
