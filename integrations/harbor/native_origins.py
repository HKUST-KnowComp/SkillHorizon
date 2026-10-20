"""Opt-in, controller-private receipts from the owned native Harbor process."""

from contextlib import contextmanager
import hashlib
from importlib import metadata
import json
import os
from pathlib import Path
import sys

from . import HARBOR_COMMIT


HARBOR_REPOSITORY = "https://github.com/harbor-framework/harbor"
HARBOR_ARCHIVE_SHA256 = "05cba1664cbb8143d09e4033769a9cb0172100f6f3c7a9879c6417127d3da4fb"
EVENTS = ("observer_entry", "observer_exit", "verifier_entry", "verifier_exit")
MODULES = ("harbor.agents.terminus_2.terminus_2",
           "harbor.agents.terminus_2.terminus_json_plain_parser", "harbor.llms.chat",
           "harbor.environments.docker.docker", "integrations.harbor.observation",
           "integrations.harbor.agent", "integrations.harbor.verifier")
OBJECTS = {"observer": {"agent": ("integrations.harbor.agent", "SkillHorizonTerminus"),
    "parser": ("harbor.agents.terminus_2.terminus_json_plain_parser", "TerminusJSONPlainParser"),
    "environment": ("harbor.environments.docker.docker", "DockerEnvironment")},
    "verifier": {"verifier": ("integrations.harbor.verifier", "SkillHorizonVerifier"),
    "environment": ("harbor.environments.docker.docker", "DockerEnvironment")}}


def required(event):
    if event not in EVENTS:
        raise ValueError("Unknown native origin event")
    return tuple(name for name in MODULES if event.startswith("verifier_") or name != "integrations.harbor.verifier")


def object_identity(event, objects):
    expected = OBJECTS[event.split("_", 1)[0]]
    if not isinstance(objects, dict) or set(objects) != set(expected):
        raise ValueError("Missing actual native objects")
    actual = {}
    for label, (module, name) in expected.items():
        cls = type(objects[label])
        if (cls.__module__ != module or cls.__qualname__ != name
                or getattr(sys.modules.get(module), name, None) is not cls):
            raise ValueError("Unexpected actual native component: " + label)
        actual[label] = {"module": module, "class": name}
    return actual


def sha(path):
    path = Path(path)
    if not path.is_file():
        raise ValueError("Expected ordinary origin file")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load(path):
    return json.loads(Path(path).read_text())


def exclusive(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x") as stream:
        json.dump(value, stream, sort_keys=True, allow_nan=False)


def tree_hash(root):
    files = {}
    for path in sorted(Path(root).rglob("*")):
        if path.is_symlink():
            raise ValueError("Export symlink forbidden")
        if path.is_file() and "__pycache__" not in path.parts:
            files[path.relative_to(root).as_posix()] = sha(path)
    return hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def installation():
    dist = metadata.distribution("harbor")
    direct = json.loads(dist.read_text("direct_url.json") or "null")
    if dist.version != "0.23.0" or not isinstance(direct, dict):
        raise ValueError("Unexpected Harbor installation")
    if set(direct) == {"url", "vcs_info"}:
        vcs = direct["vcs_info"]
        if (direct["url"] not in (HARBOR_REPOSITORY, HARBOR_REPOSITORY + ".git")
                or not isinstance(vcs, dict) or vcs.get("vcs") != "git"
                or vcs.get("commit_id") != HARBOR_COMMIT):
            raise ValueError("Unexpected Harbor VCS source")
    elif set(direct) == {"url", "archive_info"}:
        archive = direct["archive_info"]
        if (direct["url"] != HARBOR_REPOSITORY + "/archive/" + HARBOR_COMMIT + ".tar.gz"
                or not isinstance(archive, dict) or not archive
                or set(archive) - {"hash", "hashes"}):
            raise ValueError("Unexpected Harbor archive source")
        if "hash" in archive and archive["hash"] != "sha256=" + HARBOR_ARCHIVE_SHA256:
            raise ValueError("Unexpected Harbor archive hash")
        if "hashes" in archive and archive["hashes"] != {"sha256": HARBOR_ARCHIVE_SHA256}:
            raise ValueError("Unexpected Harbor archive hashes")
    else:
        raise ValueError("Ambiguous Harbor installation source")
    return {"version": dist.version, "direct_url": direct}


def freeze(path, config, task, spec):
    if set(spec["modules"]) != set(MODULES):
        raise ValueError("Incomplete native module inventory")
    for item in spec["modules"].values():
        if sha(item["path"]) != item["sha256"]:
            raise ValueError("Frozen native module drift")
    if installation() != spec["installation"]:
        raise ValueError("Frozen native installation drift")
    executable = str(Path(sys.executable).resolve())
    cli = str(Path(sys.executable).parent / "harbor")
    if (spec["executable"] != executable or spec["executable_sha256"] != sha(executable)
            or spec["cli"] != cli or spec["cli_sha256"] != sha(cli)):
        raise ValueError("Frozen native runtime drift")
    exclusive(path, {"schema": "native_cli_origins_v1", "spec": spec,
        "config": str(config), "config_sha256": sha(config),
        "task": str(task), "export_identity": tree_hash(task)})


def spawn_recorder(manifest):
    def record(process, argv):
        exclusive(Path(manifest).with_suffix(".spawn.json"), {
            "manifest_sha256": sha(manifest), "config_sha256": load(manifest)["config_sha256"],
            "pid": process.pid, "pgid": os.getpgid(process.pid), "argv": list(argv)})
    return record


def record(manifest, trial, event, *, objects=None):
    if event not in EVENTS:
        raise ValueError("Unknown native origin event")
    value = load(manifest)
    spec = value["spec"]
    if sha(value["config"]) != value["config_sha256"]:
        raise ValueError("Native config changed")
    needed = required(event)
    actual = {}
    for name, expected in spec["modules"].items():
        module = sys.modules.get(name)
        if module is None and name not in needed:
            continue
        filename = getattr(module, "__file__", None)
        origin = getattr(getattr(module, "__spec__", None), "origin", None)
        if not filename or Path(filename).resolve() != Path(expected["path"]).resolve():
            raise ValueError("Native loaded module differs: " + name)
        if origin is not None and Path(origin).resolve() != Path(filename).resolve():
            raise ValueError("Native module spec differs: " + name)
        actual[name] = {"path": str(Path(filename).resolve()), "sha256": sha(filename)}
        if actual[name]["sha256"] != expected["sha256"]:
            raise ValueError("Native loaded bytes differ: " + name)
    for name, module in tuple(sys.modules.items()):
        root = spec["harbor_root"] if name == "harbor" or name.startswith("harbor.") else (
            spec["adapter_root"] if name == "integrations.harbor" or name.startswith("integrations.harbor.") else None)
        if root is None:
            continue
        filename = getattr(module, "__file__", None)
        if filename and not Path(filename).resolve().is_relative_to(Path(root).resolve()):
            raise ValueError("Competing native project origin")
    executable = str(Path(sys.executable).resolve())
    if (executable != spec["executable"] or sha(executable) != spec["executable_sha256"]
            or sha(spec["cli"]) != spec["cli_sha256"] or installation() != spec["installation"]):
        raise ValueError("Native runtime differs")
    exclusive(Path(trial) / "controller_origins" / (event + ".json"), {
        "event": event, "manifest_sha256": sha(manifest), "config_sha256": value["config_sha256"],
        "export_identity": value["export_identity"], "pid": os.getpid(), "pgid": os.getpgrp(),
        "argv": list(sys.argv), "executable": executable, "modules": actual,
        "installation": spec["installation"], "required_modules": list(needed),
        "objects": object_identity(event, objects)})


@contextmanager
def scope(manifest, trial, role, *, objects=None):
    if manifest is None:
        yield
        return
    record(manifest, trial, role + "_entry", objects=objects)
    failure = None
    try:
        yield
    except BaseException as error:
        failure = error
        raise
    finally:
        try:
            record(manifest, trial, role + "_exit", objects=objects)
        except BaseException:
            if failure is None:
                raise
            # Missing exit evidence invalidates qualification without replacing
            # the native exception or a cancellation with an observer error.


def validate(manifest, trial):
    value = load(manifest)
    spawn = load(Path(manifest).with_suffix(".spawn.json"))
    spec = value["spec"]
    expected_argv = [spec["cli"], "run", "-c", value["config"]]
    if (spawn["argv"] != expected_argv or spawn["pid"] != spawn["pgid"]
            or spawn["manifest_sha256"] != sha(manifest)
            or spawn["config_sha256"] != sha(value["config"])
            or tree_hash(Path(value["task"])) != value["export_identity"]):
        raise ValueError("Owned native CLI binding differs")
    directory = Path(trial) / "controller_origins"
    if {p.name for p in directory.iterdir()} != {e + ".json" for e in EVENTS}:
        raise ValueError("Native origin event coverage differs")
    for event in EVENTS:
        row = load(directory / (event + ".json"))
        needed = required(event)
        expected_objects = {key: {"module": module, "class": name}
            for key, (module, name) in OBJECTS[event.split("_", 1)[0]].items()}
        if (row["event"] != event or row["pid"] != spawn["pid"] or row["pgid"] != spawn["pgid"]
                or row["argv"] != expected_argv or row["manifest_sha256"] != sha(manifest)
                or row["config_sha256"] != value["config_sha256"]
                or row["export_identity"] != value["export_identity"]
                or row["installation"] != spec["installation"]
                or row["executable"] != spec["executable"]
                or row.get("required_modules") != list(needed) or row.get("objects") != expected_objects
                or not set(needed) <= set(row["modules"]) <= set(MODULES)
                or any(record != spec["modules"][name] for name, record in row["modules"].items())):
            raise ValueError("Native in-process origin differs")
    return {"status": "passed", "events": list(EVENTS), "pid": spawn["pid"], "pgid": spawn["pgid"]}
