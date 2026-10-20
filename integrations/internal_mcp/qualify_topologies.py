"""One isolated domain per invocation; actual execution and independent replay."""

from skillhorizon.host_runtime import require_supported_python

require_supported_python()

import argparse
from copy import deepcopy
import importlib
from pathlib import Path

from .io import read_json, write_json
from .qualification import qualify_direct
from .structure import business_nodes, graph_metrics, trace_profile

ROOT = Path(__file__).resolve().parents[2]
DOMAINS = {
    "C09": {"module": "logisticsops", "source": "C_09_01_35", "builder": "freight_build",
        "runtime": "freight_runtime", "class": "FreightNetwork", "witness": "freight_witness", "evaluator": "freight_evaluate",
        "parameters": {"cohorts": 3, "legs": 5, "scenario": "handling_restricted"}},
    "C25": {"module": "docops", "source": "C_25_05_01", "builder": "portfolio_build",
        "runtime": "runtime", "class": "SkillPlanning", "witness": "witness",
        "parameters": {"width": 3, "depth": 5, "scenario": "expiring_reservations"}},
    "C06": {"module": "socialops", "source": "C_06_01_01", "builder": "reach_build",
        "runtime": "reach_runtime", "class": "ReachSocial", "witness": "reach_witness",
        "parameters": {"campaigns": 2, "waves": 4, "scenario": "overlapping_conversions"}},
    "C01": {"module": "c01_correspondence", "source": "C_01_01_01", "builder": "liquidity_build",
        "runtime": "runtime", "class": "SkillCorrespondence", "witness": "witness",
        "parameters": {"cases": 3, "phases": 5, "scenario": "deferred_commitment"}},
    "C47": {"module": "c47_finops", "source": "C_47_10_01", "builder": "codec_build",
        "runtime": "codec_runtime", "class": "CodecFinOps", "witness": "witness",
        "parameters": {"units": 2, "periods": 5, "resources_per_unit": 2, "scenario": "heterogeneous_links"}},
    "C50": {"module": "factoryops", "source": "C_50_05_01", "builder": "coupled_build",
        "runtime": "runtime", "class": "SkillProduction", "witness": "witness",
        "witness_options": {"efficient_reads": True},
        "parameters": {"levels": 4, "terminal_quantity": 1, "scenario": "equipment_windows"}},
    "C11": {"module": "researchops", "source": "C_11_01_01", "builder": "calibration_build",
        "runtime": "runtime", "class": "SkillLibrary", "witness": "witness",
        "witness_options": {"efficient_reads": True},
        "parameters": {"sections": 2, "rounds": 5, "studies_per_section": 1,
            "alias_copies": 0, "distractors_per_section": 0, "scenario": "study_retraction"}},
}


def qualify(identifier, output, *, parameters=None, seeds=(7, 19, 41)):
    require_supported_python()
    if output.exists():
        raise ValueError("Fresh topology evidence directory required")
    spec = deepcopy(DOMAINS[identifier])
    if parameters is not None:
        if not isinstance(parameters, dict) or set(parameters) - set(spec["parameters"]):
            raise ValueError("Only explicit domain construction parameters may be overridden")
        spec["parameters"].update(parameters)
    module = "integrations." + spec["module"]
    builder = importlib.import_module(module + "." + spec["builder"]).build
    runtime = getattr(importlib.import_module(module + "." + spec["runtime"]), spec["class"])
    witness = importlib.import_module(module + "." + spec["witness"]).run
    evaluator = importlib.import_module(module + "." + spec.get("evaluator", "evaluate")).evaluate
    rows = []
    for seed in seeds:
        task = output / f"seed_{seed}"
        public = builder(ROOT.parent / "Infinite_context_compact-main/envs" / spec["source"], task,
            seed=seed, background=False, dependency_pattern="distributed", runtime_identity="task_local_v1", **spec["parameters"])
        initial = read_json(task / "checkpoint/world.json")
        nodes = business_nodes(identifier, public, initial)
        _, events, result = qualify_direct(task, runtime, witness, evaluator, options=spec.get("witness_options"))
        rows.append({"seed": seed, "accepted": result["accepted"], "graph": graph_metrics(nodes),
            "edges": [[n["id"], p] for n in nodes for p in n["parents"]], "reference": trace_profile(events)})
        write_json(output / "PROGRESS.json", {"provider_calls": 0, "rows": rows})
        print(identifier, seed, result["accepted"], len(events), flush=True)
    write_json(output / "VERIFICATION.json", {"provider_calls": 0, "model_behavior_evidence": False,
        "pattern": "distributed", "runtime_identity": "task_local_v1", "witness_options": spec.get("witness_options", {}), "parameters": spec["parameters"], "rows": rows,
        "limitations": "Direct executed development references, not model performance or 512K admission."})


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("environment", choices=DOMAINS)
    parser.add_argument("output", type=Path)
    parser.add_argument("--parameters", type=Path, help="Explicit JSON construction override, recorded in verification")
    parser.add_argument("--seeds", type=int, nargs="+", default=[7, 19, 41])
    args = parser.parse_args()
    if not args.seeds or len(set(args.seeds)) != len(args.seeds):
        parser.error("Unique construction seeds required")
    qualify(args.environment, args.output, parameters=read_json(args.parameters) if args.parameters else None, seeds=args.seeds)
