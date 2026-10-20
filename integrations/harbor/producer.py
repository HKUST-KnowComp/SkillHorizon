"""Frozen business imports and Harbor-only overlays; no business imports here.

Receipts describe the actual runtime, not historical dependency equivalence.
The bootstrap runs with -S so no .pth/editable hooks execute before the guard.
"""

import argparse
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import re
import runpy
import stat
import sys


ENTRY_MODULES = ("integrations.harbor.grid", "integrations.harbor.lifecycle", "integrations.harbor.core7_private",
                 "integrations.harbor.native_probe", "integrations.benchmark_suite.length_c0",
                 "integrations.harbor.reference_terminal_probe",
                 "integrations.harbor.reference_cache_probe",
                 "integrations.benchmark_suite.native_batch",
                 "integrations.benchmark_suite.native_transport_audit")


class ProducerError(ValueError):
    pass


class ModuleOriginError(ImportError):
    def __init__(self, receipt):
        super().__init__("MODULE_ORIGIN_MISMATCH")
        self.receipt = receipt


def validate_producer(business_root):
    root = Path(business_root)
    if not root.is_absolute() or root != root.resolve() or not root.is_dir():
        raise ProducerError("PRODUCER_SOURCE_INVALID")
    for base in (root, root / "src"):
        for relative in ("integrations.py", "integrations.pyc", "integrations/__init__.py",
                         "integrations/__init__.pyc", "integrations/harbor",
                         "integrations/harbor.py", "integrations/harbor.pyc"):
            path = base / relative
            if path.exists() or path.is_symlink():
                raise ProducerError("PRODUCER_PACKAGE_COMPETITION")
    for relative in ("src/skillhorizon", "integrations"):
        directory = root / relative
        if not directory.is_dir() or directory != directory.resolve():
            raise ProducerError("PRODUCER_SOURCE_INVALID")
    return root


def create_adapter_overlay(destination, *, harbor_dir=None):
    """Copy only ordinary adapter source files, never tests, hidden files or keys."""
    source = Path(harbor_dir) if harbor_dir is not None else Path(__file__).resolve().parent
    if source.is_symlink() or source.absolute() != source.resolve() or not source.is_dir():
        raise ProducerError("ADAPTER_SOURCE_INVALID")
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    entries = []

    def copy_directory(directory):
        for path in sorted(directory.iterdir()):
            if path.is_symlink():
                raise ProducerError("ADAPTER_SYMLINK_FORBIDDEN")
            if path.name.startswith(".") or path.name == "__pycache__":
                continue
            mode = path.stat().st_mode
            if stat.S_ISDIR(mode):
                copy_directory(path)
                continue
            if not stat.S_ISREG(mode):
                raise ProducerError("ADAPTER_SPECIAL_FILE_FORBIDDEN")
            if path.suffix not in {".py", ".json", ".md", ".txt"} or path.name.startswith("test_"):
                continue
            if any(word in path.name.lower() for word in ("credential", "secret", "password", "token_cache", "id_rsa")):
                raise ProducerError("ADAPTER_CREDENTIAL_FILE_FORBIDDEN")
            data = path.read_bytes()
            if re.search(rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----", data):
                raise ProducerError("ADAPTER_CREDENTIAL_CONTENT_FORBIDDEN")
            relative = Path("integrations/harbor") / path.relative_to(source)
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            entries.append({"path": relative.as_posix(), "sha256": hashlib.sha256(data).hexdigest()})

    copy_directory(source)
    return {"adapter_source": str(source.resolve()), "adapter_overlay": str(destination.resolve()),
            "files": entries, "controller_only": True}


def dependency_paths():
    # Explicit directories only: never execute site.py, .pth or editable finders.
    return sorted({str(Path(path).resolve()) for path in sys.path if path
                   and Path(path).name in {"site-packages", "dist-packages"}
                   and Path(path).is_dir()})


def worker_environment(business_root, overlay):
    root = validate_producer(business_root)
    overlay = Path(overlay).resolve()
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TIKTOKEN_CACHE_DIR")
           if key in os.environ}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["PYTHONPATH"] = os.pathsep.join(map(str, (root / "src", root, overlay)))
    return env


def _owner(name, business_root, harbor_dir):
    if name == "integrations":
        return (business_root / "integrations", harbor_dir.parent)
    if name == "integrations.harbor" or name.startswith("integrations.harbor."):
        return (harbor_dir,)
    if name.startswith("integrations."):
        return (business_root / "integrations",)
    if name == "skillhorizon" or name.startswith("skillhorizon."):
        return (business_root / "src/skillhorizon",)
    return None


def _within(path, roots):
    if not isinstance(path, (str, os.PathLike)):
        return False
    resolved = Path(path).resolve()
    return any(resolved.is_relative_to(root) for root in roots)


def attest_modules(business_root, harbor_dir, *, required=()):
    """Audit loaded modules only. Suitable for split or private merged code trees."""
    root, adapter = Path(business_root).resolve(), Path(harbor_dir).resolve()
    origins, violations = {}, []
    for name, module in sorted(list(sys.modules.items())):
        spec = getattr(module, "__spec__", None)
        effective = getattr(spec, "name", name) if name == "__main__" else name
        roots = _owner(effective, root, adapter)
        if roots is None:
            continue
        files = [str(value) for value in (getattr(module, "__file__", None),
                                         getattr(spec, "origin", None)) if value is not None]
        paths = [str(value) for value in getattr(module, "__path__", ())]
        spec_paths = getattr(spec, "submodule_search_locations", None)
        if spec_paths is not None:
            paths.extend(str(value) for value in spec_paths)
        valid = bool(files or paths) and all(_within(path, roots) for path in files + paths)
        if effective == "integrations":
            valid = (not files and bool(paths)
                     and all(Path(path).resolve() in roots for path in paths))
        origins[name] = {"module": effective, "files": sorted(set(files)),
                         "package_paths": sorted(set(paths)), "valid": valid}
        if not valid:
            violations.append(name)
    violations.extend(name for name in required if name not in sys.modules)
    receipt = {"business_root": str(root), "harbor_dir": str(adapter),
               "modules": origins, "violations": sorted(set(violations)),
               "valid": not violations, "controller_only": True}
    if violations:
        raise ModuleOriginError(receipt)
    return receipt


def dependency_receipt():
    """Inventory the installed runtime; do not claim original dependencies."""
    from importlib.metadata import distributions
    installed = []
    for distribution in distributions():
        installed.append({"name": distribution.metadata.get("Name"), "version": distribution.version,
                          "location": str(Path(distribution.locate_file("")).resolve())})
    return {"executable": sys.executable, "python_version": sys.version,
            "prefix": sys.prefix, "search_path": list(sys.path),
            "loaded_module_files": {name: str(module.__file__) for name, module in sorted(list(sys.modules.items()))
                                    if getattr(module, "__file__", None)},
            "installed_distributions": sorted(installed, key=lambda item: (item["name"] or "", item["location"])),
            "historical_dependencies_attested": False, "controller_only": True}


class FrozenFinder:
    """A missing protected module is terminal, not an invitation to other finders."""

    frozen_producer_guard = True

    def __init__(self, business_root, harbor_dir):
        self.business_root = Path(business_root).resolve()
        self.harbor_dir = Path(harbor_dir).resolve()

    def find_spec(self, fullname, path=None, target=None):
        roots = _owner(fullname, self.business_root, self.harbor_dir)
        if roots is None:
            return None
        if fullname == "integrations":
            spec = importlib.machinery.ModuleSpec(fullname, loader=None, is_package=True)
            spec.submodule_search_locations = [str(root) for root in dict.fromkeys(roots)]
            return spec
        if fullname == "skillhorizon":
            parent = self.business_root / "src"
        elif fullname == "integrations.harbor":
            parent = self.harbor_dir.parent
        elif fullname.startswith("integrations.harbor."):
            parent = self.harbor_dir.joinpath(*fullname.split(".")[2:-1])
        elif fullname.startswith("integrations."):
            parent = self.business_root.joinpath(*fullname.split(".")[:-1])
        else:
            parent = (self.business_root / "src").joinpath(*fullname.split(".")[:-1])
        spec = importlib.machinery.PathFinder.find_spec(fullname, [str(parent)], target)
        if spec is None:
            raise ModuleNotFoundError("FROZEN_MODULE_UNAVAILABLE: " + fullname, name=fullname)
        paths = list(spec.submodule_search_locations or ())
        if spec.origin is not None:
            paths.append(spec.origin)
        if not paths or not all(_within(value, roots) for value in paths):
            raise ModuleOriginError({"valid": False, "violations": [fullname],
                                     "controller_only": True})
        return spec


def require_isolated_worker(business_root, harbor_dir):
    root, adapter = Path(business_root).resolve(), Path(harbor_dir).resolve()
    if not sys.flags.no_site or not any(
            getattr(finder, "frozen_producer_guard", False)
            and finder.business_root == root and finder.harbor_dir == adapter
            for finder in sys.meta_path):
        raise ProducerError("WORKER_IMPORT_ISOLATION_REQUIRED")


def bootstrap(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--overlay", type=Path, required=True)
    parser.add_argument("--dependency-path", action="append", default=[])
    parser.add_argument("--entry-module", choices=ENTRY_MODULES, default=ENTRY_MODULES[0])
    parser.add_argument("worker_args", nargs=argparse.REMAINDER)
    options = parser.parse_args(argv)
    if not sys.flags.no_site:
        raise ProducerError("WORKER_IMPORT_ISOLATION_REQUIRED")
    root = validate_producer(options.source)
    overlay = options.overlay.resolve()
    adapter = overlay / "integrations/harbor"
    if Path(__file__).resolve().parent != adapter or Path.cwd().resolve() != overlay:
        raise ProducerError("WORKER_OVERLAY_MISMATCH")
    expected = [str(root / "src"), str(root), str(overlay)]
    if os.environ.get("PYTHONPATH") != os.pathsep.join(expected):
        raise ProducerError("WORKER_SEARCH_PATH_MISMATCH")
    # Under -S the remaining startup entries are stdlib paths, not site packages.
    stdlib = [path for path in sys.path[1:] if path not in expected]
    dependencies = []
    for value in options.dependency_path:
        path = Path(value)
        if (path.name not in {"site-packages", "dist-packages"} or not path.is_dir()
                or path != path.resolve()):
            raise ProducerError("DEPENDENCY_PATH_INVALID")
        dependencies.append(str(path))
    sys.path[:] = list(dict.fromkeys(expected + stdlib + dependencies))
    sys.meta_path.insert(0, FrozenFinder(root, adapter))
    attest_modules(root, adapter)
    from integrations.harbor import producer as canonical
    sys.meta_path[0] = canonical.FrozenFinder(root, adapter)
    args = options.worker_args
    sys.argv = [options.entry_module, *(args[1:] if args[:1] == ["--"] else args)]
    runpy.run_module(options.entry_module, run_name="__main__", alter_sys=True)
    canonical.attest_modules(root, adapter)


if __name__ == "__main__":
    try:
        bootstrap()
    except Exception:
        # Never expose upstream exception messages or private payloads.
        print(json.dumps({"error": "PRODUCER_BOOTSTRAP_FAILED", "provider_calls": 0}), file=sys.stderr)
        sys.exit(1)
