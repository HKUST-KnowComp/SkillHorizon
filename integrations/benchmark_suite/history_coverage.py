"""Nine canonical zero-provider qualifications under the common history contract."""

import argparse
import math
import os
from pathlib import Path
import subprocess
import sys

from skillhorizon.util import load_json, write_json
from skillhorizon.experiment.config import RunConfig
from skillhorizon.experiment.history_measurement import load_reference, contract
from .history_bands import BANDS, LIMITS, bounded_worker, disk_bytes, implementation_identity, interval, require_remote

DOMAINS = ("FieldOps", "ProcureOps", "C09", "C25", "C06", "C47", "C50", "C01", "C11")
RANGES = {"DataOps": (3, 48), "FieldOps": (8, 16), "ProcureOps": (8, 16), "C09": (4, 8),
          "C25": (4, 8), "C06": (3, 6), "C47": (3, 6), "C50": (3, 5), "C01": (3, 6), "C11": (3, 6)}


def band_for(length):
    if type(length) is not int or length <= 0:
        raise ValueError("A positive measured reference length is required")
    return next((b for b in BANDS if interval(b)[0] <= length <= interval(b)[1]), None)


def profiles(seed=20260915):
    from .catalog import resolve_construction
    from .history_bands import PROFILE
    result = {d: resolve_construction(d, "small", seed=seed) for d in DOMAINS}
    result["DataOps"] = {"parameters": {**PROFILE, "seed": seed, "periods": 3}, "axis": "periods", "axis_value": 3}
    return {d: {**p, "calibration_axis_bounds": list(RANGES[d]),
                "range_qualified": False, "band": None} for d, p in result.items()}


def backends(profile_rows):
    from integrations.internal_mcp.reference_binding import backend_identity
    return {d: backend_identity(p["source"]) for d, p in profile_rows.items() if d.startswith("C")}


def worker(root, identifier, plan_path):
    require_remote(root)
    plan = load_json(plan_path)
    if (os.getppid() != plan["controller_pid"] or identifier not in DOMAINS
            or root != Path(plan["root"]) / identifier or plan["implementation"] != implementation_identity()
            or plan["profiles"] != profiles(plan["seed"]) or plan["limits"] != LIMITS
            or plan["backends"] != backends(plan["profiles"])):
        raise ValueError("Candidate is outside the frozen supervised coverage plan")
    from .build import build
    from .reference_policy import bound_reference, reference_actions
    from .reference_lag import from_episode
    construction = build(identifier, "small", root / "construction", seed=plan["seed"])
    if identifier.startswith("C") and construction["reference_policy"]["backend"] != plan["backends"][identifier]:
        raise ValueError("Constructed backend differs from the frozen coverage plan")
    task_root = Path(construction["task"])
    if identifier.startswith("C"):
        # Imports load domain backends; use an isolated interpreter per source tree.
        subprocess.run([sys.executable, "-m", "integrations.internal_mcp.framework", "import", identifier,
            str(root / "task"), "--bundle", str(task_root), "--trace", str(task_root / "trace.json")], check=True)
        task_root = root / "task"
    binding = bound_reference(task_root)
    actions = reference_actions(task_root)
    turns = math.ceil(len(actions) / 8)
    config = RunConfig(context_window=524288, max_output_tokens=32768, parallel_tool_calls=True,
        max_turns=turns+2, max_provider_requests=turns+66, max_tool_calls=len(actions)+1, max_total_tokens=100000000)
    write_json(root / "config.json", config.to_dict())
    subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "agents_sdk/control.py"),
        "prepare", str(root / "episode"), "--task", str(task_root), "--config", str(root / "config.json"),
        "--reference-batch-size", "8"], check=True)
    qualification = load_json(root / "episode/QUALIFICATION.json")
    if not qualification["passed"] or load_json(root / "episode/REFERENCE_CONTRACT.json") != binding:
        raise ValueError("Canonical reference qualification/binding failed")
    if bound_reference(task_root) != binding or implementation_identity() != plan["implementation"]:
        raise ValueError("Canonical source changed during execution")
    reference = load_reference(root / "episode/fixture")
    graph = construction["executed_dependencies"]
    report = {"environment": identifier, "accepted": True, "provider_calls": 0,
              "task": str(task_root), "reference_tokens": reference["reference_tokens"],
              "band": band_for(reference["reference_tokens"]), "reference": reference,
              "reference_binding": binding, "reference_batch_size": 8,
              "declared_business_graph": construction["business_obligations"], "executed_record_graph": graph,
              "current_record_graph": {"status": "unavailable", "reason": "No uniform artifact-currentness predicate for this domain"},
              "public_reference_uses": from_episode(root / "episode/fixture", [n["id"] for n in graph["nodes"]]),
              "qualification": str(root / "episode/QUALIFICATION.json"),
              "negative_controls": "Existing R3 evidence is separate; not reexecuted by this candidate"}
    write_json(root / "CANDIDATE.json", report)
    return report


def run(root, seed=20260915):
    require_remote(root)
    if root.exists():
        raise ValueError("Coverage root must be fresh")
    root.mkdir(parents=True)
    plan = {"revision": "canonical_history_coverage_v1", "root": str(root), "seed": seed,
            "controller_pid": os.getpid(), "implementation": implementation_identity(), "profiles": profiles(seed),
            "limits": LIMITS, "candidate_attempts": 9, "provider_calls": 0, "measurement": contract()}
    plan["backends"] = backends(plan["profiles"])
    write_json(root / "PLAN.json", plan)
    rows = {d: {"environment": d, "status": "not_run", "accepted": False} for d in DOMAINS}
    for identifier in DOMAINS:
        if disk_bytes(root) >= LIMITS["total_bytes"]:
            break
        target = root / identifier
        status = bounded_worker([sys.executable, "-m", "integrations.benchmark_suite.history_coverage", "worker",
            str(target), "--environment", identifier, "--plan", str(root / "PLAN.json")],
            target, root, root / f"{identifier}.log", LIMITS)
        row = (load_json(target / "CANDIDATE.json") if status["returncode"] == 0 and status["failure"] is None
               else {"environment": identifier, "accepted": False, "status": "candidate_failed"})
        rows[identifier] = {**row, "resource_status": status, "directory": str(target)}
        write_json(root / "COVERAGE.json", {"plan": plan, "rows": list(rows.values()),
            "provider_calls": 0, "complete": all(r["accepted"] for r in rows.values())})
        print(identifier, row["accepted"], row.get("reference_tokens"), flush=True)
        if status["failure"] in {"total_storage_limit", "process_group_not_quiescent"}:
            break


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("run", "worker"))
    parser.add_argument("root", type=Path)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--environment", choices=DOMAINS)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.mode == "worker":
        worker(args.root.resolve(), args.environment, args.plan)
    else:
        run(args.root.resolve(), args.seed)
