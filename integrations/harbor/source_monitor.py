"""Cache a full source attestation behind immutable lstat/module fingerprints."""

import json
import os
from pathlib import Path
import stat
import sys
import threading
from types import MappingProxyType

from .provenance import attest
from . import provenance


_MISSING = object()


def _absolute(path):
    # Do not resolve symlinks away before checking every absolute ancestor.
    return Path(os.path.abspath(path))


def _fingerprint(path, directory=False):
    value = path.lstat()
    if not (stat.S_ISDIR(value.st_mode) if directory else stat.S_ISREG(value.st_mode)):
        raise ValueError("Source inventory contains a symlink or wrong file type")
    identity = (value.st_dev, value.st_ino, value.st_mode)
    return identity if directory else identity + (value.st_size, value.st_mtime_ns, value.st_ctime_ns)


def _path_value(value):
    if value is _MISSING or value is None or isinstance(value, str):
        return value
    if isinstance(value, os.PathLike):
        return os.fsdecode(value)
    raise ValueError("Invalid module path attribute")


def _paths(value):
    if value is _MISSING or value is None:
        return value
    if isinstance(value, (str, bytes)):
        raise ValueError("Invalid module search path")
    return tuple(_path_value(item) for item in value)


def _module_fingerprint(modules):
    snapshot = []
    for name, module in sorted(modules.items()):
        spec = getattr(module, "__spec__", None)
        snapshot.append((name, id(module), _path_value(getattr(module, "__file__", _MISSING)),
                         _path_value(getattr(spec, "origin", _MISSING)),
                         _paths(getattr(module, "__path__", _MISSING)),
                         _paths(getattr(spec, "submodule_search_locations", _MISSING))))
    return tuple(snapshot)


class SourceMonitor:
    def __init__(self, code_root, *, task_root, runtime_root, entry_file, required_modules=(), modules=None):
        self._code = _absolute(code_root)
        self._task = _absolute(task_root)
        self._runtime = _absolute(runtime_root)
        self._entry = _absolute(entry_file)
        self._required = frozenset(required_modules)
        self._modules = modules
        self._lock = threading.Lock()
        self._cached_checks = 0
        self._refreshes = 0
        # Metadata parsing may import stdlib modules; finish it before freezing
        # the module table, then include those modules in the full attestation.
        for dependency in provenance.DEPENDENCIES:
            provenance.version(dependency)
        manifest_path = self._code / "CODE_ORIGINS.json"
        ancestors = {parent: _fingerprint(parent, True) for parent in manifest_path.parents}
        manifest_before = _fingerprint(manifest_path)
        manifest = json.loads(manifest_path.read_bytes())
        files = {manifest_path}
        for base, entries in ((self._code, manifest["files"]),
                              (self._task / "checkpoint", manifest.get("task_python_files", {})),
                              (self._runtime, manifest.get("task_python_files", {}))):
            if not isinstance(entries, dict):
                raise ValueError("Invalid source inventory")
            for relative in entries:
                path = Path(relative)
                if path.is_absolute() or ".." in path.parts or not path.parts:
                    raise ValueError("Source inventory path escaped its root")
                files.add(base / path)
        self._files = tuple(sorted(files))
        directories = {self._code, self._task, self._task / "checkpoint", self._runtime}
        for path in (*self._files, *directories):
            directories.update(path.parents)
        self._directories = tuple(sorted(directories))
        self._baseline = self._file_snapshot()
        if (_fingerprint(manifest_path) != manifest_before
                or any(_fingerprint(path, True) != expected for path, expected in ancestors.items())):
            raise ValueError("Source inventory changed while reading manifest")
        loaded = self._capture_modules()
        module_snapshot = _module_fingerprint(loaded)
        report = self._attest(loaded, verify_files=True)
        self._check_files()
        if _module_fingerprint(self._capture_modules()) != module_snapshot:
            raise ValueError("Module table changed during source attestation")
        self._save_modules(loaded, module_snapshot, report)
        initial = json.loads(self._report)
        initial["source_monitor"] = {"version": 1, "full_checks": 1, "cached_checks": 0,
            "module_refreshes": 0, "modules_refreshed": False, "check_kind": "full",
            "file_check": "full_sha256", "initial_file_check": "full_sha256"}
        self._full_report = json.dumps(initial, allow_nan=False)

    @property
    def full_report(self):
        return json.loads(self._full_report)

    @property
    def report(self):
        return self.full_report

    def _capture_modules(self):
        return dict(sys.modules if self._modules is None else self._modules)

    def _file_snapshot(self):
        return (tuple(_fingerprint(path, True) for path in self._directories),
                tuple(_fingerprint(path) for path in self._files))

    def _check_files(self):
        if self._file_snapshot() != self._baseline:
            raise ValueError("Frozen source or ancestor fingerprint changed")

    def _attest(self, modules, *, verify_files):
        return attest(self._code, phase="serve", task_root=self._task, runtime_root=self._runtime,
                      entry_file=self._entry, required_modules=self._required,
                      modules=modules, verify_files=verify_files)

    def _save_modules(self, loaded, snapshot, report):
        if not self._required <= report["modules"].keys():
            raise ValueError("Required modules were not attested")
        # Hold references so deleted modules cannot recycle an id in the cache.
        # Serialized reports and tuple snapshots cannot be mutated by callers.
        encoded = json.dumps(report, allow_nan=False)
        self._module_refs = MappingProxyType(loaded)
        self._module_snapshot = snapshot
        self._report = encoded

    def check(self, *, required_modules=None):
        with self._lock:
            if required_modules is not None and frozenset(required_modules) != self._required:
                raise ValueError("Required module scope changed")
            self._check_files()
            loaded = self._capture_modules()
            snapshot = _module_fingerprint(loaded)
            refreshed = snapshot != self._module_snapshot
            if refreshed:
                report = self._attest(loaded, verify_files=False)
            else:
                report = json.loads(self._report)
            self._check_files()
            if _module_fingerprint(self._capture_modules()) != snapshot:
                raise ValueError("Module table changed during source check")
            if not self._required <= report["modules"].keys():
                raise ValueError("Required modules were not attested")
            if refreshed:
                self._save_modules(loaded, snapshot, report)
                self._refreshes += 1
            self._cached_checks += 1
            report["source_monitor"] = {
                "version": 1, "full_checks": 1, "cached_checks": self._cached_checks,
                "module_refreshes": self._refreshes, "modules_refreshed": refreshed,
                "check_kind": "module_refreshed" if refreshed else "cached",
                "file_check": "lstat_fingerprints", "initial_file_check": "full_sha256",
            }
            return report
