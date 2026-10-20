"""Framework contract for isolated internal-domain MCP workers, not a new agent loop."""

from skillhorizon.host_runtime import require_supported_python, check_python_executable

require_supported_python()

import json
from pathlib import Path
import subprocess
import sys

from ..util import load_json, load_jsonl


FORMAT = "skillhorizon.internal_mcp_task.v1"
DOMAINS = ("C09", "C25", "C06", "C47", "C50", "C01", "C11")
ROOT = Path(__file__).resolve().parents[3]


def catalog():
    return load_json(ROOT / "integrations/internal_mcp/catalog.json")["dependency_profiles"]


def domain_entry(identifier, task_format=None):
    entries = catalog()
    if task_format is not None:
        entries += load_json(ROOT / "integrations/internal_mcp/catalog.json").get("dependency_variants", [])
    matches = [entry for entry in entries if entry["environment_id"] == identifier
               and (task_format is None or entry["task_format"] == task_format)]
    if len(matches) != 1:
        raise ValueError("Unknown or excluded internal MCP domain")
    return matches[0]


def worker(*arguments):
    check_python_executable(sys.executable)
    result = subprocess.run([sys.executable, "-m", "integrations.internal_mcp.framework", *map(str, arguments)],
                            cwd=ROOT, capture_output=True, text=True)
    if result.returncode:
        raise ValueError("Internal MCP worker failed: " + result.stderr[-4000:])
    return json.loads(result.stdout)


def validate_task(task, root):
    from ..benchmark_spec import BENCHMARK_REVISION
    from .tasks import validate_project_history
    root = Path(root)
    domain = domain_entry(task["config"]["environment_id"], task["environment_format"])
    protocol = task.get("skill_protocol")
    if (task.get("benchmark_revision") != BENCHMARK_REVISION or protocol not in {"observational_v1", "skill_declaration_v1"}
            or task.get("environment_format") != domain["task_format"]):
        raise ValueError("Internal MCP framework contract mismatch")
    environment = root / "checkpoint/environment"
    public = load_json(environment / "public/task.json")
    if public["format"] != task["environment_format"] or public["task_id"] != task["task_id"]:
        raise ValueError("Internal environment/task mismatch")
    if protocol == "skill_declaration_v1":
        declaration_profiles = {
            ("C25", "skillhorizon.c25.portfolio_network.v2"): "planning",
            ("C06", "skillhorizon.c06.reach_network.v2"): "social",
            ("C09", "skillhorizon.c09.freight_network.v2"): "connection_state",
            ("C11", "skillhorizon.c11.calibration_network.v2"): "library",
            ("C50", "skillhorizon.c50.coupled_tolerance.v2"): "production",
        }
        state_key = declaration_profiles.get((task["config"]["environment_id"], task["environment_format"]))
        names = [s["name"] for s in public["skills"]]
        actual = {path.parent.name for path in (environment / "public/skills").glob("*/SKILL.md")}
        if (state_key is None
                or public.get("policy", {}).get("skill_protocol") != protocol
                or not names or len(set(names)) != len(names)
                or not isinstance(task.get("skill_ids"), list)
                or any(not isinstance(name, str) or not name for name in task["skill_ids"])
                or len(task["skill_ids"]) != len(names) or set(task["skill_ids"]) != set(names)
                or set(names) != actual
                or any(t["name"] in {"begin_skill", "finish_skill", "select_skill_run", "read_skill_run"}
                       for t in task.get("tools", []))):
            raise ValueError("Declaration task requires the decoupled domain and complete public skill library")
        state = load_json(root / "checkpoint/world.json")[state_key]
        if "skill_runs" in state or "active_skill_run" in state:
            raise ValueError("Declaration checkpoint retains business skill bookkeeping")
        if state_key == "social":
            from integrations.socialops.declaration_build import LOCAL_GOALS
            from integrations.socialops.runtime import SkillSocial
            checkpoint = load_json(root / "checkpoint/world.json")
            original = load_json(environment / "checkpoint/world.json")
            private = load_json(environment / "private/initial_obligations.json")
            if not all((root / name).is_file() for name in ("REFERENCE_ORIGIN.json", "REFERENCE_CONTRACT.json")):
                raise ValueError("C06 declaration task requires its imported reference binding")
            if (type(task.get("checkpoint_action_count")) is not int or task["checkpoint_action_count"] != 0
                    or type(task["config"].get("prefix_calls")) is not int or task["config"]["prefix_calls"] != 0
                    or type(checkpoint.get("sequence")) is not int or checkpoint["sequence"] != 0
                    or checkpoint != original or checkpoint.get("format") != task["environment_format"]
                    or checkpoint.get("task_id") != task["task_id"]
                    or public.get("runtime_identity") != "task_local_v1" or private.get("public") != public
                    or set(names) != set(LOCAL_GOALS)):
                raise ValueError("C06 declarations require a matching fresh Reach V2 checkpoint and full library")
            scripts = {skill["name"]: {name for name in skill["files"] if name.endswith(".py")}
                       for skill in public["skills"]}
            expected = {name: {SkillSocial.script_files[name]} if name in SkillSocial.script_files else set()
                        for name in LOCAL_GOALS}
            if scripts != expected or any(not (environment / "public/skills" / skill / name).is_file()
                                          for skill, paths in scripts.items() for name in paths):
                raise ValueError("C06 declaration scripts differ from the complete public library")
        if state_key == "connection_state":
            from integrations.logisticsops.declaration_build import LOCAL_GOALS
            from integrations.logisticsops.declaration_runtime import declaration_state_clean
            checkpoint = load_json(root / "checkpoint/world.json")
            original = load_json(environment / "checkpoint/world.json")
            private = load_json(environment / "private/initial_obligations.json")
            if not all((root / name).is_file() for name in ("REFERENCE_ORIGIN.json", "REFERENCE_CONTRACT.json")):
                raise ValueError("C09 declaration task requires its imported reference binding")
            if (type(task.get("checkpoint_action_count")) is not int or task["checkpoint_action_count"] != 0
                    or type(task["config"].get("prefix_calls")) is not int or task["config"]["prefix_calls"] != 0
                    or type(checkpoint.get("sequence")) is not int or checkpoint["sequence"] != 0
                    or checkpoint != original or checkpoint.get("format") != task["environment_format"]
                    or checkpoint.get("task_id") != task["task_id"]
                    or public.get("runtime_identity") != "task_local_v1" or private.get("public") != public
                    or not declaration_state_clean(checkpoint) or set(names) != set(LOCAL_GOALS)):
                raise ValueError("C09 declarations require a matching fresh Freight V2 checkpoint and full library")
            scripts = {skill["name"]: {name for name in skill["files"] if name.endswith(".py")}
                       for skill in public["skills"]}
            expected = {name: {"scripts/check_connections.py"} if name == "plan-reaccommodation" else set()
                        for name in LOCAL_GOALS}
            if scripts != expected or any(not (environment / "public/skills" / skill / name).is_file()
                                          for skill, paths in scripts.items() for name in paths):
                raise ValueError("C09 declaration scripts differ from the complete public library")
        if state_key in {"library", "production"}:
            if state_key == "library":
                from integrations.researchops.declaration_build import LOCAL_GOALS, SCRIPTS
            else:
                from integrations.factoryops.declaration_build import LOCAL_GOALS, SCRIPTS
            identifier = task["config"]["environment_id"]
            checkpoint = load_json(root / "checkpoint/world.json")
            original = load_json(environment / "checkpoint/world.json")
            private = load_json(environment / "private/initial_obligations.json")
            if not all((root / name).is_file() for name in ("REFERENCE_ORIGIN.json", "REFERENCE_CONTRACT.json")):
                raise ValueError(f"{identifier} declaration task requires its imported reference binding")
            if (type(task.get("checkpoint_action_count")) is not int or task["checkpoint_action_count"] != 0
                    or type(task["config"].get("prefix_calls")) is not int or task["config"]["prefix_calls"] != 0
                    or type(checkpoint.get("sequence")) is not int or checkpoint["sequence"] != 0
                    or checkpoint != original or checkpoint.get("format") != task["environment_format"]
                    or checkpoint.get("task_id") != task["task_id"]
                    or public.get("runtime_identity") != "task_local_v1" or private.get("public") != public
                    or private.get("initial_state") != checkpoint[state_key]
                    or private.get("initial_tables") != checkpoint["tables"] or set(names) != set(LOCAL_GOALS)):
                raise ValueError(f"{identifier} declarations require a matching fresh checkpoint and full library")
            expected = {name: {"SKILL.md"} | ({SCRIPTS[name]} if name in SCRIPTS else set()) for name in LOCAL_GOALS}
            declared = {skill["name"]: skill["files"] for skill in public["skills"]}
            for name, paths in expected.items():
                folder = environment / "public/skills" / name
                actual_files = {path.relative_to(folder).as_posix() for path in folder.rglob("*") if path.is_file()}
                if (len(declared[name]) != len(paths) or set(declared[name]) != paths or actual_files != paths
                        or any(path.is_symlink() for path in folder.rglob("*"))):
                    raise ValueError(f"{identifier} declaration files differ from the complete public skill library")
    elif public.get("policy", {}).get("skill_protocol", "observational_v1") != protocol:
        raise ValueError("Internal public skill protocol mismatch")
    if "instance_identity" in task:
        from integrations.internal_mcp.structure import instance_identity
        if task["instance_identity"] != instance_identity(task["config"]["environment_id"], public):
            raise ValueError("Internal instance identity mismatch")
    if (task["config"].get("scenario") != public.get("scenario")
            or task.get("structure", {}).get("scenario") != public.get("scenario")):
        raise ValueError("Internal scenario provenance mismatch")
    if load_json(environment / "controller.json")["source"] != "backend":
        raise ValueError("Framework tasks require a bundled internal backend")
    build = load_json(root / "BUILD.json")
    if build.get("status") != "complete" or build["config"] != task["config"]:
        raise ValueError("Internal task import is incomplete")
    if len(task.get("messages", [])) < 2 or not task.get("tools"):
        raise ValueError("Missing public MCP request")
    validate_project_history(task, load_jsonl(root / "checkpoint/model_skill_actions.jsonl"))
    return task


def import_task(identifier, output, *, prefix_calls=0, bundle=None, trace=None):
    require_supported_python()
    arguments = ["import", identifier, Path(output).resolve(), "--prefix-calls", str(prefix_calls)]
    if bundle is not None:
        arguments += ["--bundle", Path(bundle).resolve()]
    if trace is not None:
        arguments += ["--trace", Path(trace).resolve()]
    return worker(*arguments)
