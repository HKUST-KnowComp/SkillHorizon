"""Bounded, zero-provider qualification of existing declaration trajectories."""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from .backend import write_record
from .business_view import full_inventory
from .claim_measurement import PROTOCOL, consumer_for, digest, read
from .client import Client
from .private_gateway import PrivateGateway
from .producer import attest_modules, dependency_paths, require_isolated_worker
from .smoke_control import terminate_group, termination_signals


LIMITS = {"C25-normal": (117, 7), "C25-recovery": (251, 7),
          "DataOps-normal": (567, 5), "DataOps-recovery": (825, 8),
          "FieldOps-normal": (620, 17), "FieldOps-recovery": (1113, 21),
          "C06-normal": (430, 24), "C06-recovery": (430, 24)}
CELL_SECONDS, SHARED_SECONDS = 1400, 11200
EIGHT_LABELS = tuple(LIMITS)
C06_LABELS = ("C06-normal", "C06-recovery")
REMAINING_LIMITS = {"C09-normal": (421, 27), "C09-recovery": (534, 35),
                    "C11-normal": (421, 24), "C11-recovery": (429, 24),
                    "C50-normal": (197, 22), "C50-recovery": (227, 25)}
REMAINING_LABELS = tuple(REMAINING_LIMITS)


def counts(label):
    require(label in LIMITS or label in REMAINING_LIMITS, "Unknown qualification cell")
    return LIMITS[label] if label in LIMITS else REMAINING_LIMITS[label]


def cell_budget(label):
    counts(label)
    return 1800 if label in REMAINING_LIMITS else CELL_SECONDS


def require(condition, message):
    if not condition:
        raise ValueError(message)


def tree_identity(root):
    root = Path(root)
    require(root.is_dir() and root == root.resolve(), "Ordinary absolute tree required")
    files = {}
    for path in sorted(root.rglob("*")):
        require(not path.is_symlink(), "Symlinks are not qualification inputs")
        if "__pycache__" in path.parts:
            continue
        if path.is_file():
            files[path.relative_to(root).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            require(path.is_dir(), "Special file in input tree")
    require(bool(files), "Empty input tree")
    return digest(files)


def inputs_identity(row):
    result = {key: tree_identity(Path(row[key])) for key in ("source", "task")}
    for key in ("trace", "claims", "expected"):
        path = Path(row[key])
        require(path == path.resolve() and path.is_file(), "Ordinary absolute input required")
        result[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    result["historical_evaluation"] = hashlib.sha256(
        Path(row["claims"]).with_name("evaluation.json").read_bytes()).hexdigest()
    if row["label"].startswith("C06-") or row["label"] in REMAINING_LIMITS:
        path = Path(row["historical_business"])
        require(path.is_absolute() and path == path.resolve() and path.is_file(), "Ordinary business report required")
        result["historical_business"] = hashlib.sha256(path.read_bytes()).hexdigest()
    if row["label"] in REMAINING_LIMITS:
        from .qualification_binding import file_hash, formal_binding, supplement_binding
        formal_binding(row, counts(row["label"])[0])
        for key in ("historical_status", "historical_generation"):
            result[key] = file_hash(row[key])
        if row["label"] == "C09-normal":
            result["historical_supplement"] = supplement_binding(row)
    if "original_task" in row:
        original, copied = Path(row["original_task"]), Path(row["task"])
        require(not original.is_relative_to(copied) and not copied.is_relative_to(original),
                "Original task and copy must be disjoint")
        result["original_task_full"] = digest(full_inventory(original))
        result["task_full"] = digest(full_inventory(copied))
        require(result["original_task_full"] == result["task_full"], "Original task and copy differ")
    return result


def historical_success(row):
    if row["label"] in REMAINING_LIMITS:
        from .qualification_binding import formal_binding
        return formal_binding(row, counts(row["label"])[0])
    if row["label"].startswith("C06-"):
        task = read(Path(row["task"]) / "task.json")
        require(task.get("format") == "skillhorizon.internal_mcp_task.v1"
                and task.get("config", {}).get("environment_id") == "C06", "C06 task required")
        report = read(row["historical_business"])
        identity = task.get("task_id")
        require(isinstance(identity, str) and bool(identity) and report.get("task_id") == identity,
                "Historical business task identity differs")
        result = report.get("accepted")
        require(type(result) is bool, "Historical accepted must be a boolean")
        return result
    result = read(Path(row["claims"]).with_name("evaluation.json"))["business"].get("task_success")
    require(type(result) is bool or result is None, "Invalid historical outcome")
    return result


def reserve_output(output, protected):
    output = Path(output)
    require(output.is_absolute() and output == output.resolve(), "Absolute unaliased output required")
    for path in protected:
        path = Path(path).resolve()
        require(not output.is_relative_to(path) and not path.is_relative_to(output), "Output overlaps protected inputs")
    output.mkdir(parents=True, exist_ok=False)


def trace_rows(path):
    return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]


def canonical_actions(rows):
    return [{key: row[key] for key in ("skill_id", "arguments", "result")} for row in rows]


def same_json(left, right):
    return digest(left) == digest(right)


def protected_inputs(row):
    protected = [row["source"], row["task"], *(str(Path(row[k]).parent) for k in ("trace", "claims", "expected"))]
    protected.extend(row[key] for key in ("historical_business", "historical_status", "historical_generation",
                     "historical_supplement", "original_candidate", "original_task") if key in row)
    return protected


def validate_inputs(row, actions, claims, expected, reference):
    calls, completed = counts(row["label"])
    require(len(actions) == calls and len(claims) == completed, "Qualification count mismatch")
    actual, original = canonical_actions(actions), canonical_actions(reference)
    if row["label"].startswith("C25-"):
        require(same_json(actual[:-1], original) and actual[-1]["skill_id"] == "read_publication",
                "C25 qualification must add exactly its frozen final read")
    else:
        require(same_json(actual, original), "Qualification differs from the reference")
    require(expected.get("valid") is True and len(expected["claims"]) == completed, "Invalid expected measurement")
    identity = lambda c: digest([c["run_id"], c["skill_id"], c["last_resolved_business_dispatch"]])
    require(len({c["run_id"] for c in claims}) == completed, "Duplicate fixture run")
    require(Counter(identity(c) for c in claims) == Counter(identity(c) for c in expected["claims"]),
            "Expected measurement does not match declaration fixtures")
    for claim in claims:
        cut = claim["last_resolved_business_dispatch"]
        require(type(cut) is int and 0 <= cut <= calls, "Invalid completion cut")
    require(type(row["historical_success"]) is bool or row["historical_success"] is None,
            "Historical business outcome must be explicit")


def drive(client, actions, claims, progress, *, deadline, clock=time.monotonic):
    """One ordered pass; a failed or uncertain request is never resubmitted."""
    def call(name, arguments, counter):
        remaining = deadline - clock()
        require(remaining > 0, "Live phase deadline exceeded")
        client.timeout = min(120, remaining)
        progress[counter] += 1
        return client.call(name, arguments)

    by_cut = {}
    for claim in claims:
        arguments = {key: claim[key] for key in ("run_id", "skill_id", "scope") if key in claim}
        receipt = call("begin_skill", arguments, "declaration_requests")
        require(receipt == {"run_id": claim["run_id"], "status": "open"}, "Begin declaration was not accepted")
        by_cut.setdefault(claim["last_resolved_business_dispatch"], []).append(claim)

    def finish(cut):
        # Preserve the fixture list order for declarations sharing a completion cut.
        for claim in by_cut.get(cut, []):
            args = {key: claim[key] for key in ("run_id", "outputs", "note") if key in claim}
            args["status"] = "completed"
            receipt = call("finish_skill", args, "declaration_requests")
            require(receipt == {"run_id": claim["run_id"], "status": "completed"},
                    "Finish declaration was not accepted")
            progress["completed_declarations"] += 1

    finish(0)
    for index, action in enumerate(actions, 1):
        result = call(action["skill_id"], action["arguments"], "business_requests")
        require(same_json(result, action["result"]), "Public result differs from the frozen trajectory")
        progress["matched_business_results"] += 1
        finish(index)


def validate_reference(root):
    """Use the paired complete validators, including declaration import lineage."""
    from integrations.benchmark_suite import reference_policy
    from skillhorizon.experiment import reference_trace
    from skillhorizon.util import load_json
    root = Path(root)
    task = load_json(root / "task.json")
    require(consumer_for(task) is not None, "Unsupported reference qualification route")
    modules = {reference_policy.__name__, reference_trace.__name__}
    if task["format"] == "skillhorizon.internal_mcp_task.v1":
        from integrations.internal_mcp import reference_binding
        modules.add(reference_binding.__name__)
        saved = load_json(root / "REFERENCE_CONTRACT.json")
        policy = reference_trace.current_callable_policy(saved["policy"])
        actions = reference_policy.reference_actions(root)
        lineage = reference_binding.imported_lineage(root, task["tools"], actions, policy)
        binding = reference_trace.validate_task(root, task, actions, policy, lineage=lineage)
        mode = "paired_internal_declaration_primitives"
    else:
        binding = reference_policy.bound_reference(root)
        mode = "paired_bound_reference"
    return {"mode": mode, "modules": sorted(modules), "contract_digest": digest(binding)}


def prepare_cell(row_path, output):
    row = read(row_path)
    require_isolated_worker(row["source"], Path(__file__).resolve().parent)
    reserve_output(output, protected_inputs(row))
    stage = "inputs"
    def phase(value):
        nonlocal stage
        stage = value
        write_record(output / "PHASE.json", {"phase": stage, "status": "running", "provider_calls": 0})
    try:
        phase("inputs")
        before = inputs_identity(row)
        require(before == row["inputs"], "Frozen input identity changed")
        phase("reference")
        validation = validate_reference(row["task"])
        from integrations.benchmark_suite.reference_policy import reference_actions
        phase("fixtures")
        actions, document, expected = trace_rows(row["trace"]), read(row["claims"]), read(row["expected"])
        historical = historical_success(row)
        require(row["historical_success"] is historical, "Historical business outcome differs")
        require(document.get("valid") is True and document.get("schema") == PROTOCOL, "Invalid claim fixture")
        validate_inputs(row, actions, document["claims"], expected, reference_actions(row["task"]))
        phase("export")
        from .export import export_task
        export_task(row["task"], output / "export", max_calls=len(actions), require_reference_contract=True)
        admission = None
        if read(Path(row["task"]) / "task.json")["format"] == "skillhorizon.internal_mcp_task.v1":
            phase("private_task_admission")
            from .task_admission import admit
            admission = admit(output / "export/environment/business", output / "PRIVATE_TASK_ADMISSION.json")
        phase("origins")
        require(inputs_identity(row) == before, "Inputs changed during export")
        receipt = attest_modules(row["source"], Path(__file__).resolve().parent, required=set(validation["modules"]))
        prepared = {"row": row, "export_identity": tree_identity(output / "export"),
                    "producer": receipt, "adapter_identity": tree_identity(Path(__file__).resolve().parent),
                    "reference_validation": validation, "private_task_admission": admission,
                    "source_reference_validated": True, "provider_calls": 0}
        phase("publication")
        write_record(output / "PREPARED.json", prepared)
        return prepared
    except BaseException as error:
        try:
            write_record(output / "PHASE.json", {"phase": stage, "status": "invalid",
                         "failure_type": type(error).__name__, "provider_calls": 0})
        except Exception:
            pass
        raise


def run_cell(prepared_root, output, *, deadline=None):
    prepared = read(prepared_root / "PREPARED.json")
    row = prepared["row"]
    remaining_profile = row["label"] in REMAINING_LIMITS
    started = time.monotonic()
    budget = cell_budget(row["label"])
    if deadline is None:
        deadline = started + budget
    require(type(deadline) in (int, float) and started < deadline <= started + budget,
            "Finite bounded cell deadline required")
    require_isolated_worker(row["source"], Path(__file__).resolve().parent)
    reserve_output(output, [prepared_root, *protected_inputs(row)])
    progress = {"phase": "preflight", "status": "running", "business_requests": 0,
                "matched_business_results": 0, "declaration_requests": 0, "completed_declarations": 0,
                "provider_calls": 0, "business": None, "measurement": None, "control_probes": None}
    gateway, failure = None, None
    try:
        require(inputs_identity(row) == row["inputs"], "Frozen input identity changed")
        require(tree_identity(prepared_root / "export") == prepared["export_identity"], "Export changed")
        require(tree_identity(Path(__file__).resolve().parent) == prepared["adapter_identity"], "Adapter changed")
        actions, claims = trace_rows(row["trace"]), read(row["claims"])["claims"]
        expected = read(row["expected"])
        require((len(actions), len(claims)) == counts(row["label"]), "Prepared counts changed")
        gateway = PrivateGateway(prepared_root / "export/environment/business", output / "state",
                                 max_calls=len(actions), verify_timeout=550)
        progress["control_probes"] = gateway.control_probes
        progress["phase"] = "live"
        write_record(output / "RESULT.json", progress)
        live_start = time.monotonic()
        drive(Client(gateway.url, skill_protocol=PROTOCOL), actions, claims, progress,
              deadline=min(deadline - (910 if remaining_profile else 670), live_start + 600))
        progress["live_seconds"] = time.monotonic() - live_start
        progress["phase"] = "business_verification"
        write_record(output / "RESULT.json", progress)
        phase_start = time.monotonic()
        require(deadline - time.monotonic() > (360 if remaining_profile else 120),
                "Insufficient verification and claims headroom")
        gateway.verify_timeout = min(550, deadline - time.monotonic() - (360 if remaining_profile else 120))
        require(gateway.verify_timeout > 0, "Business verification deadline exhausted")
        report = gateway.seal_and_score()
        progress["business"] = report["business"]
        progress["business_verify_seconds"] = time.monotonic() - phase_start
        require(report["gateway"]["dispatched"] == len(actions), "Live dispatch count mismatch")
        if row["historical_success"] is not None:
            require(report["business"]["task_success"] is row["historical_success"], "Business outcome changed")
        progress["phase"] = "claims"
        write_record(output / "RESULT.json", progress)
        phase_start = time.monotonic()
        claims_timeout = min(300 if remaining_profile else 120,
                             deadline - time.monotonic() - (60 if remaining_profile else 0))
        require(claims_timeout > 0, "Claims deadline exhausted")
        envelope = gateway.measure_claims(timeout=claims_timeout)
        progress["claims_seconds"] = time.monotonic() - phase_start
        progress["measurement"] = {"status": envelope["status"]}
        require(envelope["status"] == "valid", "Invalid claim measurement")
        measured = envelope["measurement"]
        progress["measurement"].update({k: measured[k] for k in (
            "completed_count", "counts", "replay_dispatches", "additional_replay_passes")})
        key = lambda c: digest([c["run_id"], c["skill_id"], c["last_resolved_business_dispatch"], c["goal_verdict"]])
        require(Counter(key(c) for c in measured["claims"]) == Counter(key(c) for c in expected["claims"]),
                "Domain claim labels changed")
        require(progress["declaration_requests"] == 2 * len(claims), "Declaration count mismatch")
        require(inputs_identity(row) == row["inputs"], "Inputs changed during execution")
        require(tree_identity(prepared_root / "export") == prepared["export_identity"], "Export changed during execution")
        progress.update(status="passed", phase="complete")
    except BaseException as error:
        failure = error
        progress.update(status="invalid", failure_type=type(error).__name__)
    finally:
        if gateway is not None:
            try:
                gateway.close()
            except BaseException as error:
                progress.update(status="invalid", cleanup_failure=type(error).__name__)
                if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                    failure = error
        progress["elapsed_seconds"] = time.monotonic() - started
        try:
            write_record(output / "RESULT.json", progress)
        except BaseException as error:
            if failure is None or (isinstance(failure, Exception) and not isinstance(error, Exception)):
                failure = error
    if failure is not None:
        raise failure
    return progress


def inspect_row(row):
    counts(row["label"])
    historical = historical_success(row)
    return {**row, "historical_success": historical, "inputs": inputs_identity(row)}


def launch_cell(row, output, shared_deadline):
    overlay = Path(__file__).resolve().parents[2]
    label = row["label"]
    now = time.monotonic()
    require(type(shared_deadline) in (int, float) and now < shared_deadline <= now + SHARED_SECONDS,
            "Finite bounded shared deadline required")
    deadline = min(now + cell_budget(label), shared_deadline)
    prepared_root = Path(row["prepared"])
    prepared = read(prepared_root / "PREPARED.json")
    require(prepared["row"]["label"] == label, "Prepared cell identity differs")
    source = Path(prepared["row"]["source"])
    command = [sys.executable, "-B", "-S", str(overlay / "integrations/harbor/producer.py"),
               "--source", str(source), "--overlay", str(overlay)]
    for path in dependency_paths():
        command.extend(["--dependency-path", path])
    command.extend(["--entry-module", "integrations.harbor.core7_private", "--", "cell",
                    str(prepared_root), str(output / label), "--deadline", str(deadline)])
    env = {"PATH": os.environ["PATH"], "HOME": os.environ["HOME"],
           "PYTHONDONTWRITEBYTECODE": "1",
           "PYTHONPATH": os.pathsep.join((str(source / "src"), str(source), str(overlay)))}
    if "TIKTOKEN_CACHE_DIR" in os.environ:
        env["TIKTOKEN_CACHE_DIR"] = os.environ["TIKTOKEN_CACHE_DIR"]
    with (output / (label + ".log")).open("xb") as log:
        require(time.monotonic() < deadline, "Shared deadline expired before launch")
        child = subprocess.Popen(command, cwd=overlay, env=env, stdin=subprocess.DEVNULL,
                                 stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        primary = None
        try:
            code = child.wait(timeout=max(0, deadline - time.monotonic()))
        except BaseException as error:
            primary = error
        try:
            terminate_group(child)
        except BaseException as error:
            if primary is None or (isinstance(primary, Exception) and not isinstance(error, Exception)):
                primary = error
        if primary is not None:
            raise primary
    return {"label": label, "returncode": code, "result": read(output / label / "RESULT.json")}


def run_batch(manifest_path, output, *, c06_only=False, remaining_only=False):
    manifest = read(manifest_path)
    require(set(manifest) == {"version", "cells"} and type(manifest["version"]) is int
            and manifest["version"] == 1, "Unknown batch manifest")
    rows = manifest["cells"]
    require(not (c06_only and remaining_only), "Qualification profiles are exclusive")
    labels = list(REMAINING_LABELS if remaining_only else C06_LABELS if c06_only else EIGHT_LABELS)
    require([row["label"] for row in rows] == labels, "Exact ordered qualification manifest required")
    require(all(set(row) == {"label", "prepared"} for row in rows), "Unknown cell fields")
    protected = [manifest_path]
    for row in rows:
        prepared = Path(row["prepared"])
        source_row = read(prepared / "PREPARED.json")["row"]
        require(source_row["label"] == row["label"], "Prepared cell identity differs")
        protected.extend([prepared, *protected_inputs(source_row)])
    reserve_output(output, protected)
    started, results = time.monotonic(), []
    shared_deadline = started + (10800 if remaining_only else 2800 if c06_only else SHARED_SECONDS)
    for row in rows:
        label = row["label"]
        try:
            write_record(output / "RESULT.json", {"status": "running", "phase": label,
                                                  "cells": results, "provider_calls": 0})
            remaining = shared_deadline - time.monotonic()
            require(remaining > 0, "Shared wall budget exhausted")
            result = launch_cell(row, output, shared_deadline)
            results.append(result)
            require(result["returncode"] == 0 and result["result"].get("status") == "passed",
                    "Cell failed; no retry or continuation")
        except BaseException as error:
            try:
                write_record(output / "RESULT.json", {"status": "invalid", "phase": label,
                    "failure_type": type(error).__name__, "cells": results, "provider_calls": 0})
            except Exception:
                pass
            raise
    write_record(output / "RESULT.json", {"status": "passed", "cells": results,
                 "provider_calls": 0, "elapsed_seconds": time.monotonic() - started})
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("inspect", "prepare", "cell", "run", "run_c06", "run_remaining"))
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--deadline", type=float)
    args = parser.parse_args()
    require(args.mode == "cell" or args.deadline is None, "Deadline override only belongs to a cell")
    with termination_signals():
        if args.mode == "inspect":
            require(not args.output.exists(), "Inspection output exists")
            write_record(args.output, inspect_row(read(args.input)))
        elif args.mode == "prepare":
            prepare_cell(args.input, args.output)
        elif args.mode == "cell":
            run_cell(args.input, args.output, deadline=args.deadline)
        else:
            run_batch(args.input, args.output, c06_only=args.mode == "run_c06",
                      remaining_only=args.mode == "run_remaining")


if __name__ == "__main__":
    main()
