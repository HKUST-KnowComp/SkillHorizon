"""Finite, diverse DataOps generation with measured native reference bands."""

import argparse
from collections import Counter, defaultdict
from contextlib import contextmanager, nullcontext
import fcntl
import json
import math
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time

from integrations.harbor.backend import write_record
from .native_reference_length import BAND_REVISION, BANDS, band_label, contract


REVISION = "native_diverse_batch_v1"
LABELS = tuple(f"{band}k" for band in BANDS)
PARAMETERS = ("periods", "regions", "rows_per_region")


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def default_profiles():
    groups = {
        "compact": [(8, 2, 3), (8, 2, 4), (8, 2, 6)],
        "temporal": [(12, 2, 3), (12, 3, 6), (16, 2, 3)],
        "regional": [(8, 3, 3), (8, 3, 6), (8, 4, 3)],
        "data_dense": [(8, 2, 12), (8, 2, 24), (8, 3, 12)],
    }
    return [{"id": f"p{p}-r{r}-n{n}", "stratum": group,
             "parameters": dict(zip(PARAMETERS, (p, r, n)))}
            for group, values in groups.items() for p, r, n in values]


def validate_profiles(profiles):
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("A nonempty profile pool is required")
    ids, configurations = set(), set()
    for profile in profiles:
        if set(profile) != {"id", "stratum", "parameters"}:
            raise ValueError("Profile fields differ")
        for key in ("id", "stratum"):
            if not isinstance(profile[key], str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", profile[key]):
                raise ValueError("Invalid profile or stratum identifier")
        params = profile["parameters"]
        if set(params) != set(PARAMETERS):
            raise ValueError("Expected periods, regions and rows_per_region")
        for name, low, high in (("periods", 8, 24), ("regions", 2, 32), ("rows_per_region", 3, 10000)):
            if type(params[name]) is not int or not low <= params[name] <= high:
                raise ValueError("Profile outside current receivables scenario bounds")
        signature = tuple(params[key] for key in PARAMETERS)
        if profile["id"] in ids or signature in configurations:
            raise ValueError("Duplicate profile id or business configuration")
        ids.add(profile["id"])
        configurations.add(signature)


def make_plan(*, count, seeds, max_candidates, jobs=1, profiles=None, max_per_profile=None):
    profiles = default_profiles() if profiles is None else profiles
    validate_profiles(profiles)
    for value in (count, max_candidates, jobs):
        if type(value) is not int or value < 1:
            raise ValueError("Positive integer counts required")
    if jobs > 2:
        raise ValueError("This generation release supports at most two workers")
    if not isinstance(seeds, list) or not seeds or any(type(seed) is not int for seed in seeds):
        raise ValueError("Explicit integer seed sequence required")
    if len(set(seeds)) != len(seeds):
        raise ValueError("Duplicate seeds")
    if max_candidates > len(profiles) * len(seeds):
        raise ValueError("Candidate limit exceeds unique profile/seed combinations")
    maximum = math.ceil(count / 4) if max_per_profile is None else max_per_profile
    if type(maximum) is not int or not 1 <= maximum <= count:
        raise ValueError("Invalid per-profile cap")
    grouped = defaultdict(list)
    for profile in profiles:
        grouped[profile["stratum"]].append(profile)
    strata = list(grouped)
    order = [grouped[group][i] for i in range(max(map(len, grouped.values())))
             for group in strata if i < len(grouped[group])]
    # Rotating seeds avoids confounding the first parallel wave with one seed.
    candidates = [{"id": f"candidate-{i:05d}", "profile": profile["id"],
                   "seed": seeds[(round_index + slot) % len(seeds)]}
                  for i, (round_index, slot, profile) in enumerate(
                      (r, s, p) for r in range(len(seeds)) for s, p in enumerate(order))][:max_candidates]
    quotas = {group: count // len(strata) + (index < count % len(strata))
              for index, group in enumerate(strata)}
    return {"revision": REVISION, "environment": "DataOps", "scenario": "receivables_rollover",
            "band_revision": BAND_REVISION, "count_per_band": count, "jobs": jobs,
            "profiles": profiles, "seeds": seeds, "max_candidates": max_candidates,
            "max_per_profile_per_band": maximum, "stratum_quotas_per_band": quotas,
            "candidates": candidates, "external_provider_calls": 0}


def validate_plan(plan):
    expected = make_plan(count=plan["count_per_band"], seeds=plan["seeds"],
                         max_candidates=plan["max_candidates"], jobs=plan["jobs"],
                         profiles=plan["profiles"], max_per_profile=plan["max_per_profile_per_band"])
    if plan != expected:
        raise ValueError("Plan differs from the finite generation contract")
    return plan


@contextmanager
def lock(path):
    with Path(path).open("a+") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Generation worker or controller is already active") from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def run_candidate(root, candidate, profile, *, lease_fd=None):
    from skillhorizon.dataflow.declaration_tasks import build_declaration_dataflow
    from integrations.harbor.reference_run import run as run_native
    from integrations.harbor import smoke_control
    from .native_transport_audit import run as audit_transport
    from .resource_sample import ResourceSamples

    validate_profiles([profile])
    root = Path(root).resolve()
    if lease_fd is None:
        root.mkdir(parents=True, exist_ok=False)
    elif (read(root / "START.json") != candidate
          or os.fstat(lease_fd).st_ino != (root / "worker.lock").stat().st_ino):
        raise ValueError("Candidate worker lease differs")
    started = time.monotonic()
    report = {"id": candidate["id"], "profile": profile["id"], "stratum": profile["stratum"],
              "parameters": profile["parameters"], "seed": candidate["seed"],
              "status": "running", "phase": "construct", "band_revision": BAND_REVISION,
              "external_provider_calls": 0}
    write_record(root / "STATUS.json", report)
    with lock(root / "worker.lock") if lease_fd is None else nullcontext():
        # The independent Harbor CLI must retain the lease if this worker dies.
        previous_lease = smoke_control.NATIVE_WORKER_LEASE_FD
        smoke_control.NATIVE_WORKER_LEASE_FD = lease_fd
        resources = ResourceSamples(root, root / "native/harbor-task/environment")
        interruption = None
        try:
            resources.start()
            task = build_declaration_dataflow(root / "task", seed=candidate["seed"],
                **profile["parameters"], family="regional_sales", notice_policy="source_only",
                business_program="credit_sales_v2", business_scenario="receivables_rollover")
            report.update(phase="native_reference", construct_seconds=round(time.monotonic() - started, 3),
                          task_id=task["task_id"], structure=task["structure"])
            write_record(root / "STATUS.json", report)
            native_started = time.monotonic()
            measured = run_native(root / "task", root / "native")
            report["native_seconds"] = round(time.monotonic() - native_started, 3)
            report.update(native_status=measured["status"], business_success=measured["business_success"],
                          native_complete=measured["native_complete"], fixture_failure=measured.get("fixture_failure"))
            if (measured["status"] != "passed" or measured["business_success"] is not True
                    or measured["native_complete"] is not True or measured["external_provider_calls"] != 0):
                raise ValueError("Native reference did not qualify")
            report["phase"] = "transport_audit"
            write_record(root / "STATUS.json", report)
            audit = audit_transport(root / "native", root / "audit")
            if audit["status"] != "passed":
                raise ValueError("Native terminal transport audit failed")
            report.update(status="passed", phase="complete", L_native_ref=measured["L_native_ref"],
                L_business_ref=measured["L_business_ref"], band=band_label(measured["L_native_ref"]),
                reference_calls=measured["reference_calls"], native_requests=measured["native_requests"],
                page_commands=audit["page_commands"], native_complete=True, business_success=True,
                transport_audit="passed", task_source=str(root / "task"),
                harbor_task=str(root / "native/harbor-task"))
        except BaseException as error:
            report.update(status="failed", failure_type=type(error).__name__, failure_stage=report["phase"])
            report["failure_kind"] = ("business_qualification" if type(error).__name__ == "PublicQualificationError"
                                      else "infrastructure_or_unclassified")
            if isinstance(error, (KeyboardInterrupt, SystemExit)):
                interruption = error
        finally:
            smoke_control.NATIVE_WORKER_LEASE_FD = previous_lease
            try:
                sample = resources.finish()
                write_record(root / "RESOURCE.json", sample)
                clean = (not sample["errors"] and bool(sample["samples"])
                         and sample["samples"][-1].get("container_count") == 0)
                report["resource_status"] = "passed" if clean else "failed"
                if not clean:
                    report.update(status="failed", failure_type=report.get("failure_type", "ResourceCleanupFailed"))
            except BaseException as error:
                report.update(status="failed", resource_status="failed", failure_type=type(error).__name__)
            report["elapsed_seconds"] = round(time.monotonic() - started, 3)
            write_record(root / "STATUS.json", report)
        if interruption is not None:
            raise interruption
    return report


def summarize(plan, rows):
    counts = {band: 0 for band in LABELS}
    strata = {band: Counter() for band in LABELS}
    profiles = {band: Counter() for band in LABELS}
    entries = []
    for candidate in plan["candidates"]:
        row = rows.get(candidate["id"])
        if row is None:
            continue
        row = dict(row)
        band = row.get("band")
        if row["status"] != "passed":
            reason = row["status"]
        elif band not in LABELS:
            reason = "outside_bands"
        elif counts[band] >= plan["count_per_band"]:
            reason = "band_full"
        elif strata[band][row["stratum"]] >= plan["stratum_quotas_per_band"][row["stratum"]]:
            reason = "stratum_full"
        elif profiles[band][row["profile"]] >= plan["max_per_profile_per_band"]:
            reason = "profile_cap"
        else:
            reason = "selected"
            counts[band] += 1
            strata[band][row["stratum"]] += 1
            profiles[band][row["profile"]] += 1
        row["selection"] = reason
        entries.append(row)
    deficits = {band: {group: target - strata[band][group]
                       for group, target in plan["stratum_quotas_per_band"].items()} for band in LABELS}
    return {"revision": REVISION, "band_revision": BAND_REVISION,
            "complete": all(value == plan["count_per_band"] for value in counts.values()),
            "counts": counts, "deficits": deficits, "candidates_recorded": len(rows),
            "entries": entries, "external_provider_calls": 0,
            "scope": "native reference task generation; not model behavior",
            "delivery": "task_source is controller-only; load harbor_task through the isolated Harbor adapter"}


def child_command(root, candidate_id, lease_fd):
    args = ["candidate", str(root), "--lease-fd", str(lease_fd), candidate_id]
    from integrations.harbor import producer
    guards = [finder for finder in sys.meta_path if getattr(finder, "frozen_producer_guard", False)]
    if guards:
        guard = guards[0]
        overlay = guard.harbor_dir.parents[1]
        argv = [sys.executable, "-S", str(guard.harbor_dir / "producer.py"),
                "--source", str(guard.business_root), "--overlay", str(overlay)]
        for path in producer.dependency_paths():
            argv.extend(("--dependency-path", path))
        return (argv + ["--entry-module", __spec__.name, "--", *args], overlay,
                producer.worker_environment(guard.business_root, overlay))
    return ([sys.executable, "-m", "integrations.benchmark_suite.native_batch", *args],
            Path(__file__).resolve().parents[2], dict(os.environ))


def verify_saved_result(directory, row):
    native = read(directory / "native/REFERENCE_LENGTH.json")
    audit = read(directory / "audit/TRANSPORT_AUDIT.json")
    resources = read(directory / "RESOURCE.json")
    receipt = read(directory / "native/RUN_RECEIPT.json")
    if (native.get("status") != "passed" or audit.get("status") != "passed"
            or receipt.get("all_trials_valid") is not True
            or any(native.get(key) != row.get(key) for key in
                   ("L_native_ref", "L_business_ref", "reference_calls", "native_requests",
                    "business_success", "native_complete", "external_provider_calls"))
            or audit.get("public_calls") != row["reference_calls"]
            or resources.get("errors") or not resources.get("samples")
            or resources["samples"][-1].get("container_count") != 0):
        raise ValueError("Saved native qualification evidence differs")


def require_quiescent(directory):
    environment = directory / "native/harbor-task/environment"
    if environment.exists():
        result = subprocess.run(["docker", "ps", "--filter",
            f"label=com.docker.compose.project.working_dir={environment}", "--format", "{{.ID}}"],
            capture_output=True, text=True, timeout=20, check=True)
        if result.stdout.strip():
            raise RuntimeError("Interrupted candidate still has running containers")


def load_rows(root, plan):
    rows = {}
    profiles = {profile["id"]: profile for profile in plan["profiles"]}
    for candidate in plan["candidates"]:
        directory = root / "candidates" / candidate["id"]
        if not directory.exists():
            continue
        with lock(directory / "worker.lock"):
            path = directory / "STATUS.json"
            row = read(path) if path.exists() else {**candidate, "status": "interrupted"}
        if any(row.get(key) != candidate[key] for key in ("id", "profile", "seed")):
            raise ValueError("Candidate identity differs from the saved plan")
        exit_path = directory / "EXIT.json"
        if exit_path.exists():
            outcome = read(exit_path)
            row = {**row, "worker_returncode": outcome["returncode"]}
            if outcome["returncode"] != 0:
                row = {**row, "status": "failed", "failure_type": "WorkerNonzeroExit"}
        elif row["status"] == "passed":
            row = {**row, "status": "interrupted", "failure_type": "WorkerExitUnconfirmed"}
        profile = profiles[candidate["profile"]]
        if row["status"] == "passed":
            if (row.get("parameters") != profile["parameters"] or row.get("stratum") != profile["stratum"]
                    or row.get("band_revision") != BAND_REVISION
                    or row.get("band") != band_label(row["L_native_ref"])
                    or row.get("native_complete") is not True or row.get("business_success") is not True
                    or row.get("transport_audit") != "passed" or row.get("resource_status") != "passed"
                    or row.get("external_provider_calls") != 0):
                raise ValueError("Candidate qualification fields differ")
            verify_saved_result(directory, row)
        elif row["status"] == "running":
            row = {**row, "status": "interrupted"}
        if row["status"] != "passed":
            require_quiescent(directory)
        rows[candidate["id"]] = row
    return rows


def runtime_identity():
    from skillhorizon.experiment.reference_trace import dataops_policy
    from skillhorizon.util import sha256_file
    from integrations.harbor import reference_run, reference_fixture, reference_terminal_helper, config, export, smoke_control
    return {"measurement": contract(), "policy": dataops_policy(skill_protocol="skill_declaration_v1"),
            "native_limits": {"max_turns": reference_run.MAX_TURNS, "agent_seconds": reference_run.MAX_SECONDS,
                              "scope": "agent limit; construction, CLI cleanup and audit add wall time"},
            "presentation": {module.__name__: sha256_file(Path(module.__file__))
                             for module in (reference_run, reference_fixture, reference_terminal_helper, config, export, smoke_control)}}


def run_batch(root, plan, *, continue_after_failure=False):
    validate_plan(plan)
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    with lock(root / "controller.lock"):
        for filename, value in (("PLAN.json", plan), ("RUNTIME.json", runtime_identity())):
            path = root / filename
            if path.exists() and read(path) != value:
                raise ValueError("Resume requires the same plan, reference policy and native presentation")
            if not path.exists():
                write_record(path, value)
        (root / "candidates").mkdir(exist_ok=True)
        rows = {}
        stop = False
        interrupted = False
        old_handlers = {}

        def request_stop(signum, frame):
            nonlocal interrupted
            interrupted = True

        def publish(reason):
            report = summarize(plan, rows)
            clean_end = reason not in {"controller_error", "candidate_failure", "interrupted"}
            report["stop_reason"] = "target_met" if report["complete"] and clean_end else reason
            report["status"] = ("error" if reason == "controller_error" else
                "complete" if report["complete"] and clean_end else "running" if reason == "running" else "partial")
            write_record(root / "SUMMARY.json", report)
            return report

        try:
            rows = load_rows(root, plan)
            stop = any(row["status"] != "passed" for row in rows.values()) and not continue_after_failure
            for sig in (signal.SIGTERM, signal.SIGINT):
                old_handlers[sig] = signal.signal(sig, request_stop)
            while not stop and not interrupted and not summarize(plan, rows)["complete"]:
                remaining = [candidate for candidate in plan["candidates"] if candidate["id"] not in rows]
                if not remaining:
                    break
                wave = remaining[:plan["jobs"]]
                processes = []
                try:
                    for candidate in wave:
                        if interrupted:
                            break
                        directory = root / "candidates" / candidate["id"]
                        directory.mkdir()
                        # Inherit the lease so a crashed controller cannot duplicate a starting worker.
                        with (directory / "worker.lock").open("a+") as lease:
                            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                            write_record(directory / "START.json", candidate)
                            argv, cwd, env = child_command(root, candidate["id"], lease.fileno())
                            if interrupted:
                                break
                            with (root / (candidate["id"] + ".log")).open("a") as log:
                                process = subprocess.Popen(argv, cwd=cwd, env=env, stdout=log,
                                    stderr=subprocess.STDOUT, pass_fds=(lease.fileno(),), start_new_session=True)
                            processes.append((candidate, process))
                    publish("running")
                    while any(process.poll() is None for _, process in processes):
                        time.sleep(1)
                finally:
                    # Stop dispatch on interruption; allow in-flight native cleanup to finish.
                    for candidate, process in processes:
                        process.wait()
                        write_record(root / "candidates" / candidate["id"] / "EXIT.json",
                                     {"returncode": process.returncode})
                rows = load_rows(root, plan)
                for candidate, process in processes:
                    row = rows.get(candidate["id"])
                    if row is None:
                        directory = root / "candidates" / candidate["id"]
                        directory.mkdir(exist_ok=True)
                        row = {**candidate, "status": "failed", "phase": "worker_start",
                               "failure_type": "WorkerExitedWithoutResult", "returncode": process.returncode}
                        write_record(directory / "STATUS.json", row)
                        rows[candidate["id"]] = row
                    if process.returncode != 0 or row["status"] != "passed":
                        stop = True
                publish("running" if not stop else "candidate_failure")
        except Exception as error:
            report = publish("controller_error")
            report["controller_error"] = type(error).__name__
            write_record(root / "SUMMARY.json", report)
            raise
        finally:
            for sig, handler in old_handlers.items():
                signal.signal(sig, handler)
        return publish("interrupted" if interrupted else "candidate_failure" if stop else "candidate_limit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    plan_parser = commands.add_parser("plan")
    plan_parser.add_argument("output", type=Path)
    plan_parser.add_argument("--count", type=int, required=True)
    plan_parser.add_argument("--seeds", type=int, nargs="+", required=True)
    plan_parser.add_argument("--max-candidates", type=int, required=True)
    plan_parser.add_argument("--jobs", type=int, default=1)
    plan_parser.add_argument("--profiles", type=Path)
    plan_parser.add_argument("--max-per-profile", type=int)
    run_parser = commands.add_parser("run")
    run_parser.add_argument("root", type=Path)
    run_parser.add_argument("--plan", type=Path, required=True)
    run_parser.add_argument("--continue-after-failure", action="store_true")
    candidate_parser = commands.add_parser("candidate")
    candidate_parser.add_argument("root", type=Path)
    candidate_parser.add_argument("candidate_id")
    candidate_parser.add_argument("--lease-fd", type=int, required=True)
    options = parser.parse_args()
    if options.command == "plan":
        plan = make_plan(count=options.count, seeds=options.seeds, max_candidates=options.max_candidates,
                         jobs=options.jobs, profiles=read(options.profiles) if options.profiles else None,
                         max_per_profile=options.max_per_profile)
        with options.output.open("x", encoding="utf-8") as stream:
            json.dump(plan, stream, ensure_ascii=False, indent=2)
        print(json.dumps({"plan": str(options.output), "candidates": len(plan["candidates"]),
                          "targets": {band: plan["count_per_band"] for band in LABELS}}))
        return
    root = options.root.resolve()
    if sys.platform != "linux" or not root.is_relative_to(Path("/data")):
        parser.error("Complete native generation runs on CPU8 under /data")
    if options.command == "run":
        result = run_batch(root, read(options.plan), continue_after_failure=options.continue_after_failure)
        print(json.dumps({key: result[key] for key in ("status", "counts", "stop_reason", "candidates_recorded")}))
        raise SystemExit(0 if result["status"] == "complete" else 2)
    plan = validate_plan(read(root / "PLAN.json"))
    candidate = next(item for item in plan["candidates"] if item["id"] == options.candidate_id)
    profile = next(item for item in plan["profiles"] if item["id"] == candidate["profile"])
    if read(root / "RUNTIME.json") != runtime_identity():
        raise ValueError("Candidate runtime differs from batch")
    try:
        result = run_candidate(root / "candidates" / candidate["id"], candidate, profile, lease_fd=options.lease_fd)
    finally:
        os.close(options.lease_fd)
    raise SystemExit(0 if result["status"] == "passed" else 1)


if __name__ == "__main__":
    main()
