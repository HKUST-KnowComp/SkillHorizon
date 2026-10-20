"""Isolated environment import, MCP execution and independent framework effect replay."""

from skillhorizon.host_runtime import require_supported_python

require_supported_python()

import argparse
import asyncio
from copy import deepcopy
import importlib
import json
from pathlib import Path
import shutil
import sys
import tempfile

from skillhorizon.benchmark_spec import BENCHMARK_REVISION
from skillhorizon.experiment.internal_mcp import FORMAT, ROOT, domain_entry
from skillhorizon.experiment.mcp_tools import canonicalize_mcp_schema, strict_compatible_mcp_schema
from skillhorizon.experiment.tasks import action_messages, validate_project_history
from skillhorizon.util import append_jsonl, load_jsonl, load_json, write_jsonl
from .io import read_json, write_json, controller_source
from .transport import serve
from .structure import instance_identity
from .outcome import require_unfinished, validate_outcome

sys.dont_write_bytecode = True


RUNTIMES = {
    "C09": ("integrations.logisticsops.network_runtime", "TransferNetwork"),
    "C25": ("integrations.docops.runtime", "SkillPlanning"),
    "C06": ("integrations.socialops.exposure_runtime", "ExposureSocial"),
    "C47": ("integrations.c47_finops.workload_runtime", "WorkloadFinOps"),
    "C50": ("integrations.factoryops.runtime", "SkillProduction"),
    "C01": ("integrations.c01_correspondence.runtime", "SkillCorrespondence"),
    "C11": ("integrations.researchops.runtime", "SkillLibrary"),
}


def runtime_for(task, environment, runtime):
    entry = domain_entry(task["config"]["environment_id"], task["environment_format"])
    if task["format"] != FORMAT or task["environment_format"] != entry["task_format"]:
        raise ValueError("Internal framework format mismatch")
    if entry["environment_id"] == "C09":
        protocol = task.get("skill_protocol", "observational_v1")
        public = read_json(Path(environment) / "public/task.json")
        if (protocol not in {"observational_v1", "skill_declaration_v1"}
                or public.get("policy", {}).get("skill_protocol", "observational_v1") != protocol):
            raise ValueError("C09 runtime/public protocol mismatch")
        if task["environment_format"] == "skillhorizon.c09.freight_network.v2":
            from integrations.logisticsops.freight_server import make_runtime
            return make_runtime(environment, runtime)
        if protocol == "skill_declaration_v1":
            raise ValueError("C09 declarations require Freight V2")
    if entry["environment_id"] == "C06":
        protocol = task.get("skill_protocol", "observational_v1")
        public = read_json(Path(environment) / "public/task.json")
        if (protocol not in {"observational_v1", "skill_declaration_v1"}
                or public.get("policy", {}).get("skill_protocol", "observational_v1") != protocol):
            raise ValueError("C06 runtime/public protocol mismatch")
        if task["environment_format"] == "skillhorizon.c06.reach_network.v2":
            from integrations.socialops.reach_server import make_runtime
            return make_runtime(environment, runtime)
        if protocol == "skill_declaration_v1":
            raise ValueError("C06 declarations require Reach V2")
    if entry["environment_id"] in {"C11", "C50"}:
        identifier = entry["environment_id"]
        expected, server = {
            "C11": ("skillhorizon.c11.calibration_network.v2", "integrations.researchops.server"),
            "C50": ("skillhorizon.c50.coupled_tolerance.v2", "integrations.factoryops.server"),
        }[identifier]
        protocol = task.get("skill_protocol", "observational_v1")
        public = read_json(Path(environment) / "public/task.json")
        if (protocol not in {"observational_v1", "skill_declaration_v1"}
                or public.get("policy", {}).get("skill_protocol", "observational_v1") != protocol):
            raise ValueError(f"{identifier} runtime/public protocol mismatch")
        if task["environment_format"] == expected:
            return importlib.import_module(server).make_runtime(environment, runtime)
        if protocol == "skill_declaration_v1":
            raise ValueError(f"{identifier} declarations require {expected}")
    module, name = RUNTIMES[entry["environment_id"]]
    module, name = entry.get("runtime_module", module), entry.get("runtime_class", name)
    return getattr(importlib.import_module(module), name)(environment, runtime)


class RecordedRuntime:
    def __init__(self, runtime, root):
        self.runtime, self.root, self.metadata = runtime, Path(root), runtime.metadata

    def call(self, name, arguments):
        output = self.runtime.call(name, arguments)
        append_jsonl(self.root / "model_skill_actions.jsonl", {"skill_id": name, "arguments": deepcopy(arguments), "result": deepcopy(output)})
        return output


def metadata_tools(runtime):
    result = []
    for name, item in runtime.metadata.items():
        schema = canonicalize_mcp_schema(item["input_schema"])
        result.append({"type": "function", "name": name, "description": item["description"], "parameters": schema,
                       "strict": strict_compatible_mcp_schema(schema)})
    return result


def import_task(identifier, output, prefix_calls=0, bundle=None, trace=None):
    require_supported_python()
    entry = domain_entry(identifier)
    if bundle is None:
        bundle = ROOT / entry["ready_bundle"]
        trace = trace or ROOT / entry.get("reference_trace", str(Path(entry["candidate_evidence"]).parent / "candidate/trace.json"))
    elif trace is None:
        raise ValueError("Custom bundles require their explicit public-tool reference trace")
    bundle, output = Path(bundle).resolve(), Path(output).resolve()
    if output.exists() or output.is_relative_to(bundle) or bundle.is_relative_to(output):
        raise ValueError("Use a fresh nonoverlapping framework task")
    public = read_json(bundle / "public/task.json")
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol not in {"observational_v1", "skill_declaration_v1"} or (
            protocol == "skill_declaration_v1" and (identifier, public["format"]) not in {
                ("C25", "skillhorizon.c25.portfolio_network.v2"),
                ("C09", "skillhorizon.c09.freight_network.v2"),
                ("C11", "skillhorizon.c11.calibration_network.v2"),
                ("C50", "skillhorizon.c50.coupled_tolerance.v2"),
                ("C06", "skillhorizon.c06.reach_network.v2")}):
        raise ValueError("Unsupported domain skill protocol")
    if protocol == "skill_declaration_v1" and identifier in {"C06", "C09", "C11", "C50"} and prefix_calls != 0:
        raise ValueError(f"{identifier} declarations currently require a zero-prefix task")
    if (protocol == "skill_declaration_v1" and identifier in {"C06", "C09", "C11", "C50"}
            and not (bundle / "REFERENCE_GENERATION.json").is_file()):
        raise ValueError(f"{identifier} declaration import requires a qualified reference generation")
    entry = domain_entry(identifier, public["format"])
    if public["format"] != entry["task_format"]:
        raise ValueError("Only the selected business-dependency profile can be imported")
    events = read_json(trace)
    if type(prefix_calls) is not int or not 0 <= prefix_calls < len(events):
        raise ValueError("Prefix must leave actual reference work unfinished")
    write_json(output / "BUILD.json", {"status": "incomplete", "provider_calls": 0,
        "stage": "checkpoint_import", "prefix_calls": prefix_calls})
    checkpoint = output / "checkpoint"
    shutil.copytree(bundle / "checkpoint", checkpoint)
    environment = checkpoint / "environment"
    for directory in ("public", "private", "checkpoint"):
        shutil.copytree(bundle / directory, environment / directory)
    if (bundle / "background.json").is_file():
        shutil.copy2(bundle / "background.json", environment / "background.json")
    source = controller_source(bundle)
    for directory in ("mcp_servers", "data"):
        for path in (source / directory).rglob("*"):
            if path.is_symlink():
                raise ValueError("Bundled backends must not contain symlinks")
            if path.is_file() and path.suffix in {".py", ".json"}:
                destination = environment / "backend" / path.relative_to(source)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, destination)
    write_json(environment / "controller.json", {"source": "backend", "provider_calls": 0})
    config = {"environment_id": identifier, "profile": "dependencies", "prefix_calls": prefix_calls}
    if "scenario" in public:
        config["scenario"] = deepcopy(public["scenario"])
    task = {"format": FORMAT, "benchmark_revision": BENCHMARK_REVISION, "task_id": public["task_id"],
            "instance_identity": instance_identity(identifier, public),
            "environment_format": public["format"], "skill_protocol": protocol, "config": config,
            "execution_mode": "native_mcp", "checkpoint_action_count": prefix_calls,
            "history_source": "executed_public_tool_reference_prefix",
            "structure": {"environment_id": identifier, "skill_count": len(public["skills"]),
                          "reference_tool_calls": len(events), "reference_is_not_minimum_horizon": True}}
    if protocol == "skill_declaration_v1":
        task["skill_ids"] = [skill["name"] for skill in public["skills"]]
        if not task["skill_ids"] or len(set(task["skill_ids"])) != len(task["skill_ids"]):
            raise ValueError("A complete unique public skill library is required")
    if "scenario" in public:
        task["structure"]["scenario"] = deepcopy(public["scenario"])
    # The source is a sibling of the mutable work directory, never inside a backend's mutable root.
    working = output / "import_work"
    shutil.copytree(bundle / "checkpoint", working)
    runtime = RecordedRuntime(runtime_for(task, environment, working), working)
    task["tools"] = metadata_tools(runtime)
    generation = None
    if (bundle / "REFERENCE_GENERATION.json").is_file():
        from .reference_binding import validate, backend_identity
        generation = validate(bundle, task["tools"], events)
        if backend_identity(environment / "backend") != generation["backend"]:
            raise ValueError("Imported backend differs from the qualified reference source")
    for index, event in enumerate(events[:prefix_calls]):
        if runtime.call(event["name"], event["arguments"]) != event["output"]:
            raise ValueError(f"Reference prefix differs at tool call {index + 1}")
    for path in working.iterdir():
        destination = checkpoint / path.name
        if destination.exists():
            if destination.is_dir():
                shutil.rmtree(destination)
            else:
                destination.unlink()
        shutil.move(str(path), str(destination))
    working.rmdir()
    if not (checkpoint / "model_skill_actions.jsonl").exists():
        write_jsonl(checkpoint / "model_skill_actions.jsonl", [])
    actions = load_jsonl(checkpoint / "model_skill_actions.jsonl")
    instructions = {"role": "user", "content": "Complete this synthetic " + entry["domain"] + " project using its MCP tools. "
        + public["goal"] + " Read the public task and relevant skill instructions as needed. The recorded history contains completed actions, not a required future plan. "
        "Skill bookkeeping records your choices; it does not authorize business actions or establish business success."}
    if protocol == "skill_declaration_v1":
        instructions["content"] += (" Declare skill use with begin_skill and finish_skill through the client. "
            "completed means you believe the declared local goal has been achieved; blocked or abandoned do not claim completion. "
            "Declarations do not authorize or perform business work. Scope and outputs are optional public {kind,id} references. "
            "Use a fresh run_id for new work, and optionally associate individual business calls using the client's skill_run_id envelope.")
    task["completion_contract"] = "autonomous_business_completion_v1"
    task["public_parameter_contract"] = "public_static_enums_v1"
    task["messages"] = [instructions, *action_messages(actions, "internal-history"),
                        {"role": "user", "content": "Carry out the remaining work from the current environment state and complete the entire requested project. "
                         "A plan, opened skill run, or progress report is not completion. Keep using the available tools until the requested business deliverables "
                         "and required closing steps are done. If genuinely unable to proceed, explain the concrete blocker and unfinished work."}]
    evaluation = None
    stage = "checkpoint_admission"
    try:
        evaluation = evaluate(task, checkpoint, checkpoint)
        require_unfinished(identifier, evaluation)
        stage = "construction_provenance"
        from skillhorizon.construction_provenance import propagate_provenance
        metadata = propagate_provenance(bundle, output, operation="import", task=task)
    except Exception as error:
        write_json(output / "BUILD.json", {"status": "failed", "config": config, "provider_calls": 0,
            "stage": stage, "failure_type": type(error).__name__,
            "checkpoint_evaluation": evaluation,
            "scope": "Controller-only construction failure, not model behavior"})
        raise
    write_json(output / "task.json", task)
    continuation = [{"skill_id": e["name"], "arguments": e["arguments"], "result": e["output"]}
                    for e in events[prefix_calls:]]
    write_json(output / "continuation.json", continuation)
    if generation is not None:
        from skillhorizon.experiment.reference_trace import freeze_task
        if validate(bundle, task["tools"], events) != generation:
            raise ValueError("Reference lineage changed during import")
        freeze_task(output, task, continuation, generation["policy"], lineage=generation)
    write_json(output / "BUILD.json", {"status": "complete", "config": config, "provider_calls": 0,
        "prefix_exactly_reexecuted": True, "checkpoint_admitted_unfinished": True,
        "checkpoint_evaluation": evaluation, "reference_scope": "controller_only_fixture_not_model_policy", **metadata})
    return {"task_id": task["task_id"], "instance_identity": task["instance_identity"], "environment_id": identifier, "output": str(output), "prefix_calls": prefix_calls,
            "reference_suffix_calls": len(events) - prefix_calls, "provider_calls": 0}


def snapshot(root):
    result = {}
    for path in Path(root).rglob("*"):
        if not path.is_file() or path.name in {"model_skill_actions.jsonl", "observed_trace.jsonl"} or "__pycache__" in path.parts:
            continue
        relative = str(path.relative_to(root))
        if relative == "world.json":
            # Argument logging can reorder object keys without changing state.
            world = load_json(path)
            if not isinstance(world, dict):
                raise ValueError("World state must be a JSON object")
            result[relative] = json.dumps(world, sort_keys=True, ensure_ascii=True,
                                          allow_nan=False, separators=(",", ":")).encode("utf-8")
        else:
            result[relative] = path.read_bytes()
    return result


def evaluate(task, checkpoint, runtime, *, claim_observer=None, observer=None):
    from skillhorizon.experiment.managed_session import require_world_quiescent
    require_world_quiescent(Path(runtime).resolve().parent)
    c06_claims = (task.get("format") == FORMAT
                  and task["config"]["environment_id"] == "C06"
                  and task.get("environment_format") == "skillhorizon.c06.reach_network.v2"
                  and task.get("skill_protocol") == "skill_declaration_v1"
                  and type(task.get("checkpoint_action_count")) is int
                  and task["checkpoint_action_count"] == 0)
    c09_claims = (task.get("format") == FORMAT
                  and task["config"]["environment_id"] == "C09"
                  and task.get("environment_format") == "skillhorizon.c09.freight_network.v2"
                  and task.get("skill_protocol") == "skill_declaration_v1"
                  and type(task.get("checkpoint_action_count")) is int
                  and task["checkpoint_action_count"] == 0)
    c11_claims = (task.get("format") == FORMAT
                  and task["config"]["environment_id"] == "C11"
                  and task.get("environment_format") == "skillhorizon.c11.calibration_network.v2"
                  and task.get("skill_protocol") == "skill_declaration_v1"
                  and type(task.get("checkpoint_action_count")) is int
                  and task["checkpoint_action_count"] == 0)
    c50_claims = (task.get("format") == FORMAT
                  and task["config"]["environment_id"] == "C50"
                  and task.get("environment_format") == "skillhorizon.c50.coupled_tolerance.v2"
                  and task.get("skill_protocol") == "skill_declaration_v1"
                  and type(task.get("checkpoint_action_count")) is int
                  and task["checkpoint_action_count"] == 0)
    runtime_claims = c06_claims or c09_claims or c11_claims or c50_claims
    if observer is not None and not c06_claims:
        raise ValueError("Creation evidence requires a zero-prefix C06 declaration task")
    if claim_observer is not None and not (runtime_claims or (
            task["config"]["environment_id"] == "C25"
            and task["skill_protocol"] == "skill_declaration_v1")):
        raise ValueError("Claim observations require a declaration-ready C25, C06, C09, C11 or C50 task")
    checkpoint, runtime = Path(checkpoint), Path(runtime)
    initial, observed = load_jsonl(checkpoint / "model_skill_actions.jsonl"), load_jsonl(runtime / "model_skill_actions.jsonl")
    validate_project_history(task, initial)
    if observed[:len(initial)] != initial:
        return {"valid": False, "errors": ["Executed checkpoint history changed"], "task_success": False}
    errors = []
    with tempfile.TemporaryDirectory(prefix="internal-framework-replay-") as temporary:
        replay = Path(temporary) / "runtime"
        shutil.copytree(checkpoint, replay)
        backend = RecordedRuntime(runtime_for(task, checkpoint / "environment", replay), replay)
        if claim_observer is not None:
            claim_observer(0, backend.runtime if runtime_claims else backend.runtime.world)
        for index, action in enumerate(observed[len(initial):], len(initial)):
            before_world = deepcopy(backend.runtime.world) if observer is not None else None
            if backend.call(action["skill_id"], action["arguments"]) != action["result"]:
                errors.append(f"Tool effect differs at action {index + 1}")
            if observer is not None:
                observer(before_world, backend.runtime, deepcopy(action), index)
                del before_world
            if claim_observer is not None:
                claim_observer(index - len(initial) + 1, backend.runtime if runtime_claims else backend.runtime.world)
        if snapshot(replay) != snapshot(runtime):
            errors.append("Final environment differs from recorded tool execution")
    entry = domain_entry(task["config"]["environment_id"], task["environment_format"])
    evaluator = importlib.import_module(entry.get("evaluate_module", entry["module"] + ".evaluate")).evaluate
    domain = evaluator(checkpoint / "environment", runtime)
    domain_valid = True
    try:
        validate_outcome(entry["environment_id"], domain)
    except ValueError as error:
        domain_valid = False
        errors.append(str(error))
    accepted = domain_valid and domain["accepted"]
    return {"valid": not errors, "errors": errors, "task_success": not errors and accepted,
            "domain_evaluation_valid": domain_valid,
            "state": {"success": accepted, "termination_ready": accepted, "skill_protocol": task["skill_protocol"]},
            "executed_tool_calls": len(observed) - len(initial), "domain_evaluation": domain,
            "independent_content_verification": None, "independent_maintenance_verification": None,
            "scope": "Exact recorded execution and independent domain outcome; skill closure is not business success."}


def main():
    require_supported_python()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="action", required=True)
    create = sub.add_parser("import")
    create.add_argument("identifier", choices=RUNTIMES)
    create.add_argument("output", type=Path)
    create.add_argument("--prefix-calls", type=int, default=0)
    create.add_argument("--bundle", type=Path)
    create.add_argument("--trace", type=Path)
    server = sub.add_parser("serve")
    server.add_argument("task", type=Path)
    server.add_argument("runtime", type=Path)
    score = sub.add_parser("evaluate")
    score.add_argument("task_json", type=Path)
    score.add_argument("checkpoint", type=Path)
    score.add_argument("runtime", type=Path)
    args = vars(parser.parse_args())
    action = args.pop("action")
    if action == "import":
        result = import_task(**args)
    elif action == "evaluate":
        result = evaluate(read_json(args["task_json"]), args["checkpoint"], args["runtime"])
    else:
        root, runtime = args["task"].resolve(), args["runtime"].resolve()
        from skillhorizon.experiment.execution_bundle import ensure_execution_known, ExecutionGuardedBusiness
        ensure_execution_known(root)
        if (root / "manifest.json").exists():
            from skillhorizon.experiment.business_binding import business_binding
            if business_binding(root)["business_transport"] == "managed_worker":
                from skillhorizon.experiment.managed_session import serve_managed
                asyncio.run(serve_managed(root, runtime))
                return
        if runtime.exists() or root.is_relative_to(runtime) or runtime.is_relative_to(root / "checkpoint"):
            raise ValueError("MCP runtime must be fresh and separate from immutable checkpoint")
        task_file = root / ("private_task.json" if (root / "private_task.json").exists() else "task.json")
        task = read_json(task_file)
        shutil.copytree(root / "checkpoint", runtime)
        backend = RecordedRuntime(runtime_for(task, root / "checkpoint/environment", runtime), runtime)
        backend = ExecutionGuardedBusiness(root, backend)
        if (root / "manifest.json").exists():
            from skillhorizon.experiment.workspace_bundle import start_workspace, WorkspaceComposition
            workspace = start_workspace(root)
            if workspace is not None:
                # Composition includes the independently declared Python tool and
                # guards business dispatch while file effects are unknown.
                backend = WorkspaceComposition(backend, workspace)
        asyncio.run(serve(backend, "skillhorizon-internal"))
        return
    print(json.dumps(result, allow_nan=False))


if __name__ == "__main__":
    main()
