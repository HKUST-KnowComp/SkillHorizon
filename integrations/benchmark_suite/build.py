"""One isolated worker per domain/size; full reference execution, zero provider."""

from skillhorizon.host_runtime import require_supported_python

require_supported_python()

import argparse
import importlib
import inspect
from pathlib import Path

from integrations.internal_mcp.io import read_json, write_json
from integrations.internal_mcp.qualification import qualify_direct
from integrations.internal_mcp.qualify_topologies import DOMAINS
from integrations.internal_mcp.richness import inventory
from integrations.internal_mcp.structure import trace_profile
from skillhorizon.util import load_jsonl
from .catalog import OPS, catalog, resolve_construction, validate_profile, effective_parameter, MEASUREMENT_REVISION
from .horizon import intrinsic_graph, executed_graph
from .profile import check_profile
from skillhorizon.construction_provenance import construction_entry, provenance_evidence
from .reference_policy import reference_actions, policy_manifest, repeat_read_audit


def build(identifier, size, output, *, seed=20260910):
    return _build(resolve_construction(identifier, size, seed=seed), output, strict=False)


def build_profile(identifier, output, *, seed, profile, progress=None, phase_failure=None):
    return _build(validate_profile(profile, identifier=identifier, seed=seed), output,
                  strict=True, progress=progress, phase_failure=phase_failure)


def require_grid_construction(identity, profile, bundle):
    if identity["construction"]["status"] != "match":
        raise ValueError("Grid construction requires complete matching observed bindings")
    record, checks = provenance_evidence(bundle, expected_source=profile["source"])
    if (not record or not checks or any(c["status"] != "match" for c in checks)
            or not any(c["field"] == "provenance.binding" for c in checks)):
        raise ValueError("Grid construction requires actual bound provenance")
    expected = [profile["builder"], *profile["consumers"]]
    calls = record.get("calls", [])
    if not profile["consumers"] or [c.get("callable") for c in calls] != expected:
        raise ValueError("Grid construction consumer chain is incomplete")
    for index, call in enumerate(calls):
        module, name = expected[index].rsplit(".", 1)
        function = getattr(importlib.import_module(module), name)
        parameters = inspect.signature(function).parameters
        arguments = call.get("arguments", {})
        required = set(parameters) & set(profile["parameters"])
        if any(p.kind == p.VAR_KEYWORD for p in parameters.values()):
            raise ValueError("Grid consumers need explicit parameter contracts")
        if (call.get("returned") is not True or call.get("phase") != ("requested" if index == 0 else "delegated")
                or not required <= arguments.keys()):
            raise ValueError("Grid construction effective arguments are incomplete")
        for name in required:
            value = profile["parameters"][name] if index == 0 else effective_parameter(profile, name)
            if arguments[name] != value:
                raise ValueError("Grid construction effective arguments differ: " + name)


def _build(profile, output, *, strict, progress=None, phase_failure=None):
    require_supported_python()
    progress = progress or (lambda stage: None)
    progress("construction")
    if output.exists():
        raise ValueError("Construction output must be fresh")
    identifier, size, seed = profile["environment_id"], profile["size"], profile["parameters"]["seed"]
    parameters = profile["parameters"]
    module, name = profile["builder"].rsplit(".", 1)
    builder = construction_entry(getattr(importlib.import_module(module), name))
    bundle = output / "task"
    public_qualification = None
    if identifier in OPS:
        if identifier == "DataOps":
            from skillhorizon.dataflow.verification import verify_release_content
            public = builder(bundle, **parameters)
            from skillhorizon.dataflow.runtime import DataflowRuntime as Runtime
            progress("business_verification")
            content = verify_release_content(bundle / "reference")
            if not content["passed"]:
                raise ValueError("Independent source-only content verification failed")
        else:
            from skillhorizon.fieldops.runtime import OperationsRuntime as Runtime
            public = builder(bundle, **parameters)
        progress("business_verification")
        initial = read_json(bundle / "initial/project.json")
        reference_root = bundle / ("public_qualification/runtime" if identifier == "DataOps" else "reference")
        final = read_json(reference_root / "project.json")
        evaluation = Runtime(reference_root).score()
        if not evaluation["success"]:
            raise ValueError("Reference business verification failed")
        if identifier == "DataOps":
            public_qualification = read_json(bundle / "public_qualification/QUALIFICATION.json")
            if public_qualification.get("accepted") is not True:
                raise ValueError("Independent public DataOps qualification failed")
        actions = reference_actions(bundle)
        events = [{"name": r["skill_id"], "arguments": r["arguments"], "output": r["result"]} for r in actions]
        reference = {"reference_calls": len(events), "reference_skill_stages": sum(a["skill_id"] == "finish_skill" for a in actions),
            "skill_bookkeeping_and_read_calls": sum(e["name"] in {"begin_skill", "finish_skill", "read_skill_file", "abandon_skill", "revise_skill"} for e in events),
            "scope": "Executed public procedural reference, not actor behavior or a shortest solution"}
        visible = None
    else:
        from .programs import binding
        spec = binding(identifier, profile["environment_format"])
        module = "integrations." + spec["module"]
        runtime = getattr(importlib.import_module(module + "." + spec["runtime"]), spec["class"])
        witness = importlib.import_module(module + "." + spec["witness"]).run
        evaluator = importlib.import_module(module + "." + spec.get("evaluator", "evaluate")).evaluate
        public = builder(Path(profile["source"]), bundle, **parameters)
        initial = read_json(bundle / "checkpoint/world.json")
        if public["format"] != profile["environment_format"] or initial["format"] != public["format"]:
            raise ValueError("Profile, returned public and actual world programs differ")
        progress("business_verification")
        env, events, evaluation = qualify_direct(bundle, runtime, witness, evaluator,
            options=spec.get("witness_options"), progress=progress, phase_failure=phase_failure)
        progress("reference_structure")
        final = env.world
        reference = trace_profile(events)
        progress("reference_inventory")
        visible = inventory(bundle, public, initial, events)
    progress("profile_binding")
    identity = check_profile(profile, bundle)
    if strict:
        require_grid_construction(identity, profile, bundle)
    progress("construction_metrics")
    report = {"environment": identifier, "size": size, "seed": seed, "axis": profile["axis"], "axis_value": profile["axis_value"],
        "measurement_contract": MEASUREMENT_REVISION, "construction_profile": profile, "profile_check": identity,
        "scope": "construction_development", "provider_calls": 0, "model_behavior_evidence": False,
        "task": str(bundle.resolve()), "skill_protocol": profile["skill_protocol"],
        "business_obligations": intrinsic_graph(identifier, public, initial),
        "executed_dependencies": executed_graph(identifier, final),
        "reference": reference, "public_qualification": public_qualification, "observed_inventory": visible,
        "reference_policy": read_json(bundle / ("REFERENCE_CONTRACT.json" if identifier in OPS else "REFERENCE_GENERATION.json")),
        "repeat_read_audit": repeat_read_audit(events),
        "actor_trajectory": None, "accepted": identity["construction"]["status"] != "mismatch", "evaluation": evaluation}
    progress("construction_report_write")
    write_json(output / "MEASUREMENTS.json", report)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", choices=[r["environment_id"] for r in catalog()["environments"]])
    parser.add_argument("size", choices=("small", "medium", "long"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=20260910)
    parser.add_argument("--verify-task", type=Path, help="Compare an existing task; do not construct or execute")
    parser.add_argument("--plan", type=Path, help="Frozen run PLAN for --verify-task")
    parser.add_argument("--export-bundle", type=Path, help="Existing exported bundle for --verify-task")
    args = parser.parse_args()
    if args.verify_task is not None:
        if args.output.exists():
            raise ValueError("Profile report output must be fresh")
        result = check_profile(resolve_construction(args.environment, args.size, seed=args.seed), args.verify_task,
            plan=read_json(args.plan) if args.plan else None, export_bundle=args.export_bundle)
        write_json(args.output / "PROFILE_CHECK.json", result)
        print(result["classification"], flush=True)
    else:
        if args.plan is not None or args.export_bundle is not None:
            parser.error("--plan and --export-bundle require --verify-task")
        result = build(args.environment, args.size, args.output.resolve(), seed=args.seed)
        print(result["environment"], result["size"], result["accepted"], result["reference"]["reference_calls"], flush=True)
