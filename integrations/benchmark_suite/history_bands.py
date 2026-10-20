"""Measured reference-history bands, generated through real zero-provider execution."""

import argparse
import math
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time

from skillhorizon.util import load_json, sha256_tree, write_json
from skillhorizon.experiment.history_measurement import K, contract, load_reference
from .reference_policy import policy_manifest

ROOT = Path(__file__).resolve().parents[2]
BANDS = (64, 128, 256, 512)
PROFILE = {"family": "regional_sales", "business_program": "credit_sales_v2",
           "business_scenario": "credit_horizon", "regions": 2, "rows_per_region": 6,
           "execution_mode": "procedural", "suffix_skills": None, "notice_policy": "source_only"}
LIMITS = {"candidates": 12, "candidate_seconds": 1800, "candidate_bytes": 4 * 1024**3,
          "total_bytes": 32 * 1024**3, "axis_min": 3, "axis_max": 48, "reference_batch_size": 8}
SCENARIO_CONSTANTS = {"credit_source": "customer-credit-limits", "customer": "C01",
                      "correction": "floor(original_limit/3)", "trigger": "P02 period_settlement_ready",
                      "invoice_delays": [1, 2, 3], "beyond_horizon": "remain outstanding"}


def implementation_identity():
    return {folder: sha256_tree(ROOT / folder, excluded_directory_names={"__pycache__", ".pytest_cache"})
            for folder in ("src", "integrations")}


def interval(band):
    if band not in BANDS:
        raise ValueError("Unknown history band")
    target = band * K
    return (target * 85 + 99) // 100, target * 115 // 100


def measured(row):
    value = row.get("reference_tokens")
    return row.get("accepted") is True and type(value) is int and value > 0


def selected(rows, band):
    low, high = interval(band)
    matches = [r for r in rows if measured(r) and low <= r["reference_tokens"] <= high]
    return min(matches, key=lambda r: (abs(r["reference_tokens"]-band*K), r["axis_value"])) if matches else None


def monotonic(rows):
    valid = sorted((r for r in rows if measured(r)), key=lambda r: r["axis_value"])
    return all(a["reference_tokens"] <= b["reference_tokens"] for a, b in zip(valid, valid[1:]))


def boundary_skip(rows, band, limits=LIMITS):
    if selected(rows, band) is not None or not monotonic(rows):
        return None
    low, high = interval(band)
    for row in rows:
        if not measured(row):
            continue
        if row["axis_value"] == limits["axis_min"] and row["reference_tokens"] > high:
            return {"status": "lower_axis_limit_observed", "axis_value": row["axis_value"], "tokens": row["reference_tokens"]}
        if row["axis_value"] == limits["axis_max"] and row["reference_tokens"] < low:
            return {"status": "upper_axis_limit_observed", "axis_value": row["axis_value"], "tokens": row["reference_tokens"]}
    return None


def next_axis(rows, band, limits=LIMITS):
    attempted = {r["axis_value"] for r in rows}
    available = [n for n in range(limits["axis_min"], limits["axis_max"]+1) if n not in attempted]
    if not available:
        return None
    valid = [r for r in rows if measured(r)]
    if not valid or not monotonic(rows):
        return available[0]
    target = band * K
    below = [r["axis_value"] for r in valid if r["reference_tokens"] < target]
    above = [r["axis_value"] for r in valid if r["reference_tokens"] >= target]
    if below and above and max(below) < min(above):
        proposed = (max(below)+min(above)) // 2
        inside = [n for n in available if max(below) < n < min(above)]
        if inside:
            return min(inside, key=lambda n: (abs(n-proposed), n))
    last = valid[-1]
    proposed = max(limits["axis_min"], min(limits["axis_max"], math.ceil(last["axis_value"]*target/last["reference_tokens"])))
    return min(available, key=lambda n: (abs(n-proposed), n))


def require_remote(path):
    if sys.platform != "linux" or socket.gethostname() != "research-host" or not Path(path).resolve().is_relative_to("/data"):
        raise ValueError("Large construction requires an explicit CPU8 /data directory")


def disk_bytes(root):
    total = 0
    for path in Path(root).rglob("*"):
        try:
            if path.is_file() and not path.is_symlink():
                total += path.stat().st_size
        except FileNotFoundError:
            # Atomic JSON writers can replace a temporary file during a scan.
            continue
    return total


def close_group(process):
    def alive():
        try:
            os.killpg(process.pid, 0)
            return True
        except ProcessLookupError:
            return False
    if process.pid == os.getpgrp():
        raise ValueError("Refuse to signal the controller group")
    for sig, allowance in ((signal.SIGTERM, .2), (signal.SIGKILL, 5)):
        if alive():
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
        end = time.monotonic() + allowance
        while alive() and time.monotonic() < end:
            process.poll()
            time.sleep(.01)
    return process.poll() is not None and not alive()


def bounded_worker(argv, target, root, log, limits, *, env=None):
    started = time.monotonic()
    containment = None
    if sys.platform == "linux":
        from .descendants import Containment
        containment = Containment()
    with log.open("w") as stream:
        try:
            process = subprocess.Popen(argv, cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT, start_new_session=True, env=env)
        except BaseException:
            if containment:
                containment.restore()
            raise
        reason = None
        peak_rss = 0
        peak_log = 0
        samples = 0
        monitor_error = None
        try:
            while process.poll() is None:
                samples += 1
                peak_log = max(peak_log, log.stat().st_size)
                if "rss_bytes" in limits:
                    from .descendants import descendant_rss
                    peak_rss = max(peak_rss, descendant_rss())
                if time.monotonic()-started > limits["candidate_seconds"]:
                    reason = "candidate_timeout"
                elif peak_rss > limits.get("rss_bytes", float("inf")):
                    reason = "candidate_rss_limit"
                elif peak_log > limits.get("log_bytes", float("inf")):
                    reason = "candidate_log_limit"
                elif disk_bytes(target) + log.stat().st_size > limits["candidate_bytes"]:
                    reason = "candidate_storage_limit"
                elif disk_bytes(root) > limits["total_bytes"]:
                    reason = "total_storage_limit"
                if reason:
                    break
                time.sleep(2)
        except Exception as error:
            reason = "resource_monitor_failed"
            monitor_error = type(error).__name__
        finally:
            first_failure = reason
            cleanup_started = time.monotonic()
            quiescent = containment.close(process) if containment else close_group(process)
            cleanup_seconds = time.monotonic()-cleanup_started
    candidate_bytes, total_bytes = disk_bytes(target) + log.stat().st_size, disk_bytes(root)
    peak_log = max(peak_log, log.stat().st_size)
    if not quiescent:
        reason = "process_group_not_quiescent"
    elif candidate_bytes > limits["candidate_bytes"]:
        reason = "candidate_storage_limit"
    elif total_bytes > limits["total_bytes"]:
        reason = "total_storage_limit"
    elif time.monotonic()-started > limits["candidate_seconds"]:
        reason = reason or "candidate_timeout"
    elif peak_log > limits.get("log_bytes", float("inf")):
        reason = reason or "candidate_log_limit"
    return {"returncode": process.returncode, "failure": reason,
            "quiescent": quiescent, "candidate_bytes": candidate_bytes, "total_bytes": total_bytes,
            "elapsed_seconds": time.monotonic()-started, "sampled_peak_rss_bytes": peak_rss,
            "resource_samples": samples, "monitor_error_class": monitor_error, "cleanup_seconds": cleanup_seconds,
            "first_failure": first_failure,
            "limits_exceeded": {"disk": candidate_bytes > limits["candidate_bytes"],
                "suite_disk": total_bytes > limits["total_bytes"], "log": peak_log > limits.get("log_bytes", float("inf")),
                "rss": peak_rss > limits.get("rss_bytes", float("inf"))},
            "peak_log_bytes": peak_log,
            "rss_overshoot_bytes": max(0, peak_rss-limits.get("rss_bytes", peak_rss)),
            "log_overshoot_bytes": max(0, peak_log-limits.get("log_bytes", peak_log))}


def recovery_evidence(initial, runtime):
    world = runtime.project
    changed = [key for key, row in world["sources"].items() if row["revision"] > initial["sources"][key]["revision"]]
    stale = {key for key in world["artifacts"] if not runtime.current(key)}
    groups = {}
    for key, item in world["artifacts"].items():
        group = (item["producer_skill"], item.get("period"), item.get("region"), item.get("intermediate", False))
        groups.setdefault(group, []).append(key)
    recomputed = sum(any(k in stale for k in ids) and any(runtime.current(k) for k in ids) for ids in groups.values())
    return {"changed_sources": changed, "stale_descendant_artifacts": len(stale),
            "recomputed_current_groups": recomputed,
            "consequential_recovery": bool(changed and stale and recomputed),
            "scope": "Restatement of earlier inputs; fixed early event, not increasing dependency lag"}


def worker(output, periods, seed, plan_path):
    require_remote(output)
    if plan_path is None:
        raise ValueError("Worker requires an active resource-supervising controller")
    plan = load_json(plan_path)
    if (os.getppid() != plan["controller_pid"] or output != Path(plan["root"]) / f"P{periods:02d}"
            or seed != plan["seed"] or plan["implementation"] != implementation_identity()
            or plan["profile"] != PROFILE or plan["limits"] != LIMITS
            or plan["scenario_constants"] != SCENARIO_CONSTANTS):
        raise ValueError("Worker is outside its frozen supervised construction plan")
    if output.exists():
        raise ValueError("Candidate output must be fresh")
    from skillhorizon.dataflow.tasks import build_dataflow
    from skillhorizon.dataflow.runtime import DataflowRuntime
    from skillhorizon.experiment.config import RunConfig
    from .reference_policy import reference_actions
    from .horizon import intrinsic_graph, executed_graph
    before = policy_manifest("DataOps")
    task_root = output / "task"
    task = build_dataflow(task_root, periods=periods, seed=seed, **PROFILE)
    actions = reference_actions(task_root)
    config = RunConfig(context_window=524288, compact_at=200000, max_output_tokens=32768,
                       parallel_tool_calls=True, max_turns=math.ceil(len(actions)/8)+2,
                       max_provider_requests=math.ceil(len(actions)/8)+66,
                       max_tool_calls=len(actions)+1, max_total_tokens=100000000)
    write_json(output / "config.json", config.to_dict())
    subprocess.run([sys.executable, str(ROOT / "integrations/agents_sdk/control.py"), "prepare",
                    str(output / "episode"), "--task", str(task_root), "--config", str(output / "config.json"),
                    "--reference-batch-size", "8"], check=True, cwd=ROOT)
    qualification = load_json(output / "episode/QUALIFICATION.json")
    reference = load_reference(output / "episode/fixture")
    if before != policy_manifest("DataOps") or implementation_identity() != plan["implementation"]:
        raise ValueError("Reference policy changed during qualification")
    runtime = DataflowRuntime(output / "episode/fixture_bundle/runtime")
    initial = load_json(task_root / "initial/project.json")
    recovery = recovery_evidence(initial, runtime)
    accepted = qualification["passed"] and recovery["consequential_recovery"]
    result = {"environment": "DataOps", "profile": PROFILE, "axis": "periods", "axis_value": periods,
              "seed": seed, "accepted": accepted, "provider_calls": 0,
              "implementation": plan["implementation"], "reference_batch_size": 8,
              "reference_tokens": reference["reference_tokens"] if accepted else None,
              "reference": reference, "policy": before,
              "qualification": str(output / "episode/QUALIFICATION.json"), "task": str(task_root),
              "business_obligations": intrinsic_graph("DataOps", task, initial),
              "executed_dependencies": executed_graph("DataOps", runtime.project), "recovery": recovery}
    write_json(output / "CANDIDATE.json", result)
    return result


def calibrate(root, *, seed=20260915):
    require_remote(root)
    if root.exists():
        raise ValueError("Calibration root must be fresh; retain prior candidates")
    root.mkdir(parents=True)
    plan = {"revision": "reference_history_bands_v1", "environment": "DataOps", "seed": seed,
            "root": str(root), "controller_pid": os.getpid(), "implementation": implementation_identity(),
            "scenario_constants": SCENARIO_CONSTANTS,
            "profile": PROFILE, "limits": LIMITS, "bands": {str(b): interval(b) for b in BANDS},
            "policy": policy_manifest("DataOps"), "measurement": contract(), "provider_calls": 0}
    write_json(root / "PLAN.json", plan)
    rows = []
    for band in BANDS:
        while selected(rows, band) is None and len(rows) < LIMITS["candidates"]:
            if boundary_skip(rows, band):
                break
            if disk_bytes(root) >= LIMITS["total_bytes"]:
                break
            axis = next_axis(rows, band)
            if axis is None:
                break
            target = root / f"P{axis:02d}"
            status = bounded_worker([sys.executable, "-m", "integrations.benchmark_suite.history_bands", "worker",
                str(target), "--periods", str(axis), "--seed", str(seed), "--plan", str(root / "PLAN.json")],
                target, root, root / f"P{axis:02d}.log", LIMITS)
            if status["returncode"] == 0 and status["failure"] is None:
                row = load_json(target / "CANDIDATE.json")
                if (row["policy"] != plan["policy"] or row["profile"] != PROFILE or row["seed"] != seed
                        or row["implementation"] != plan["implementation"] or row["axis_value"] != axis
                        or row["reference_batch_size"] != LIMITS["reference_batch_size"]):
                    raise ValueError("Candidate differs from frozen calibration identity")
            else:
                row = {"axis_value": axis, "accepted": False, "reference_tokens": None,
                       "status": "candidate_failed", "provider_calls": 0}
            rows.append({**row, "resource_status": status, "directory": str(target)})
            write_json(root / "PROGRESS.json", {"plan": plan, "candidates": rows})
            print(f"DataOps P{axis:02d}: accepted={row['accepted']} reference_tokens={row['reference_tokens']}", flush=True)
            if status["failure"] in {"total_storage_limit", "process_group_not_quiescent"}:
                break
        if rows and rows[-1]["resource_status"]["failure"] in {"total_storage_limit", "process_group_not_quiescent"}:
            break
    bands = {}
    for band in BANDS:
        chosen = selected(rows, band)
        bands[str(band)] = ({"status": "selected", "candidate": chosen["directory"],
                            "reference_tokens": chosen["reference_tokens"], "axis_value": chosen["axis_value"]}
                           if chosen else boundary_skip(rows, band) or {"status": "not_found_within_budget"})
    result = {"plan": plan, "bands": bands, "candidates": rows, "provider_calls": 0,
              "observed_monotonicity": monotonic(rows), "complete": all(r["status"] == "selected" for r in bands.values()),
              "scope": "Reference-history calibration, not model behavior or proof of unattainable bands"}
    write_json(root / "CALIBRATION.json", result)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("calibrate", "worker"))
    parser.add_argument("output", type=Path)
    parser.add_argument("--seed", type=int, default=20260915)
    parser.add_argument("--periods", type=int)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args()
    if args.mode == "worker":
        worker(args.output.resolve(), args.periods, args.seed, args.plan)
    else:
        calibrate(args.output.resolve(), seed=args.seed)
