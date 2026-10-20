"""Smoke trial classification and bounded, trial-scoped abnormal shutdown."""

import json
from contextlib import contextmanager
import os
from pathlib import Path
import signal
import subprocess
import time

from .backend import write_record
from .process_cleanup import terminate_group


class ShutdownRequested(BaseException):
    pass


@contextmanager
def termination_signals():
    received = False
    def stop(signum, frame):
        nonlocal received
        if not received:
            received = True
            raise ShutdownRequested("controller_signal_" + str(signum))
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def classify(directory):
    paths = list(directory.glob("*/*/result.json"))
    if len(paths) != 1:
        return {"valid_execution": False, "reason": "missing_or_multiple_trial_results"}
    result = json.loads(paths[0].read_text())
    base = {"trial_result": str(paths[0]), "exception": (result.get("exception_info") or {}).get("exception_type")}
    if not result.get("finished_at"):
        return dict(base, valid_execution=False, reason="trial_pending")
    if result.get("exception_info") is not None:
        return dict(base, valid_execution=False, reason="trial_exception")
    evidence = paths[0].parent / "business_private/evaluation.json"
    if not evidence.is_file():
        return dict(base, valid_execution=False, reason="private_evaluation_missing")
    report = json.loads(evidence.read_text())
    business, gateway = report["business"], report["gateway"]
    reward = (result.get("verifier_result") or {}).get("rewards", {}).get("reward")
    if (business.get("valid") is not True or type(business.get("task_success")) is not bool
            or gateway.get("sealed") is not True or gateway.get("execution_unknown") is not False
            or gateway.get("inflight") is not None or type(reward) not in {int, float}
            or reward != float(business["task_success"])):
        return dict(base, valid_execution=False, reason="invalid_business_evidence")
    return dict(base, valid_execution=True, reason="business_success" if business["task_success"] else "business_incomplete",
                reward=reward, actions=gateway["dispatched"], business_success=business["task_success"])


def _docker(args, timeout=30):
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)


def _running_state(identity):
    try:
        result = _docker(["inspect", "--format", "{{.State.Running}}", identity])
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    if result.returncode != 0:
        return "unknown"
    return {"true": "running", "false": "stopped"}.get(result.stdout.strip(), "unknown")


def recover_containers(task, destination, *, mode="verify", deadline=None):
    """Preserve evidence but never produce a formal reward from interrupted cleanup."""
    if mode == "stop_copy":
        from .stop_copy import recover
        return recover(task, destination, deadline=deadline, docker=_docker)
    if mode != "verify":
        raise ValueError("Unknown cleanup mode")
    destination.mkdir(parents=True, exist_ok=False)
    receipt = {"valid_for_scoring": False, "steps": [], "quiescence_confirmed": False}
    try:
        listed = _docker(["ps", "-aq", "--filter", "label=com.docker.compose.project.working_dir=" + str(task / "environment")])
        if listed.returncode:
            raise RuntimeError("container_inventory_failed")
        services = {}
        for identity in listed.stdout.split():
            inspected = _docker(["inspect", "--format", '{{index .Config.Labels "com.docker.compose.service"}}', identity])
            if inspected.returncode or inspected.stdout.strip() in services:
                raise RuntimeError("container_identity_unverified")
            services[inspected.stdout.strip()] = identity
        if set(services) != {"main", "business"}:
            raise RuntimeError("expected_containers_missing")
        stopped = _docker(["stop", "--time", "10", services["main"]], timeout=20)
        receipt["steps"].append({"step": "stop_main", "return_code": stopped.returncode})
        maximum = json.loads((task / "migration.json").read_text())["max_calls"]
        running = _running_state(services["business"])
        if running == "running":
            try:
                seal = _docker(["exec", services["business"], "python", "-m", "integrations.harbor.backend",
                    "verify", "/task", "/state", "--max-calls", str(maximum)], timeout=180)
                receipt["steps"].append({"step": "seal", "return_code": seal.returncode})
            except subprocess.TimeoutExpired:
                receipt["steps"].append({"step": "seal", "error": "timeout"})
        else:
            receipt["steps"].append({"step": "seal", "status": "skipped",
                "skip_reason": "already_stopped" if running == "stopped" else "state_unknown"})
        copied = _docker(["cp", services["business"] + ":/state/.", str(destination / "state")], timeout=45)
        receipt["steps"].append({"step": "collect_private", "return_code": copied.returncode})
    except Exception as error:
        receipt["error_type"] = type(error).__name__
    finally:
        for identity in (services.values() if "services" in locals() else []):
            try:
                result = _docker(["stop", "--time", "10", identity], timeout=20)
                receipt["steps"].append({"step": "stop", "container": identity, "return_code": result.returncode})
            except Exception as error:
                receipt["steps"].append({"step": "stop", "container": identity, "error": type(error).__name__})
        try:
            receipt["quiescence_confirmed"] = set(services) == {"main", "business"} and all(
                _running_state(identity) == "stopped"
                for identity in services.values())
        except Exception:
            pass
        write_record(destination / "RECOVERY.json", receipt)
    return receipt


NATIVE_WORKER_LEASE_FD = None


def run_process(command, environment, log, task, recovery, proxy, timeout=2400,
                *, cleanup_mode="verify", deadline=None, cleanup_deadline=None, spawn_record=None):
    from .stop_copy import remaining
    if cleanup_mode not in {"verify", "stop_copy"}:
        raise ValueError("Unknown cleanup mode")
    if cleanup_mode == "stop_copy" and (deadline is None or cleanup_deadline is None):
        raise ValueError("Bounded stop/copy process requires both deadlines")
    process = None
    try:
        remaining(deadline, timeout)
        lease = () if NATIVE_WORKER_LEASE_FD is None else (NATIVE_WORKER_LEASE_FD,)
        process = subprocess.Popen(command, env=environment, stdout=log, stderr=subprocess.STDOUT,
                                   start_new_session=True, pass_fds=lease)
        if spawn_record is not None:
            spawn_record(process, command)
        code = process.wait(timeout=remaining(deadline, timeout))
        if cleanup_mode == "stop_copy":
            terminate_group(process, deadline=deadline)
        else:
            terminate_group(process)
        return code
    except BaseException:
        errors = []
        cleanup_end = min(cleanup_deadline, time.monotonic() + 90) if cleanup_mode == "stop_copy" else None
        try:
            proxy.stop_new_requests()
        except BaseException as error:
            errors.append(type(error).__name__)
        try:
            if process is None:
                raise ProcessLookupError()
            if cleanup_mode == "stop_copy":
                terminate_group(process, deadline=cleanup_end)
            else:
                terminate_group(process)
        except BaseException as error:
            errors.append(type(error).__name__)
        try:
            if cleanup_mode == "stop_copy":
                recover_containers(task, recovery, mode=cleanup_mode,
                                   deadline=cleanup_end)
            else:
                recover_containers(task, recovery)
        except BaseException as error:
            errors.append(type(error).__name__)
        try:
            write_record(recovery / "CONTROLLER_SHUTDOWN.json", {"errors": errors, "valid_for_scoring": False})
        except BaseException:
            pass
        raise
