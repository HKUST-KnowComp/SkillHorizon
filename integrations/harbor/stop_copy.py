"""Bounded capture of existing container state; never run a verifier in cleanup."""

import json
import math
from pathlib import Path
import time

from .backend import write_record


def remaining(deadline, cap):
    if (type(cap) not in {int, float} or not math.isfinite(cap) or cap <= 0
            or (deadline is not None and (type(deadline) not in {int, float} or not math.isfinite(deadline)))):
        raise ValueError("Finite positive cap and finite absolute deadline required")
    value = min(cap, deadline - time.monotonic()) if deadline is not None else cap
    if value <= 0:
        raise TimeoutError("Absolute qualification deadline expired")
    return value


def recover(task, destination, *, deadline, docker):
    task, destination = Path(task).resolve(), Path(destination)
    if deadline is None:
        raise ValueError("Stop/copy cleanup requires an absolute deadline")
    # A partial capture is not permission for a new attempt or a fresh budget.
    destination.mkdir(parents=True, exist_ok=False)
    receipt = {"mode": "stop_copy", "task": str(task), "deadline": deadline,
               "valid_for_scoring": False, "quiescence_confirmed": False, "steps": []}
    services = {}

    def call(args, cap=20):
        return docker(args, timeout=remaining(deadline, cap))

    def state(identity):
        result = call(["inspect", "--format", "{{.State.Running}}", identity])
        return result.returncode == 0 and result.stdout.strip() == "false"

    failure = None
    try:
        listed = call(["ps", "-aq", "--filter",
            "label=com.docker.compose.project.working_dir=" + str(task / "environment")])
        if listed.returncode:
            raise RuntimeError("Container inventory failed")
        for identity in listed.stdout.split():
            try:
                result = call(["inspect", "--format", '{{json .Config.Labels}}', identity])
                labels = json.loads(result.stdout) if result.returncode == 0 else {}
                service = labels.get("com.docker.compose.service")
                if (service not in {"main", "business"} or service in services
                        or labels.get("com.docker.compose.project.working_dir") != str(task / "environment")):
                    raise RuntimeError("Container ownership mismatch")
                services[service] = identity
            except Exception as error:
                if failure is None:
                    failure = error
        if set(services) != {"main", "business"}:
            raise RuntimeError("Expected owned containers missing")
    except BaseException as error:
        if failure is None or not isinstance(error, Exception):
            failure = error
    try:
        failures = []
        for service in ("main", "business"):
            if service not in services:
                continue
            try:
                result = call(["stop", "--time", "10", services[service]])
                receipt["steps"].append({"step": "stop", "service": service,
                    "container": services[service], "return_code": result.returncode})
                if result.returncode:
                    failures.append(service)
            except BaseException as error:
                if failure is None or not isinstance(error, Exception):
                    failure = error
                failures.append(service)
                receipt["steps"].append({"step": "stop", "service": service,
                                          "error": type(error).__name__})
        receipt["quiescence_confirmed"] = set(services) == {"main", "business"} and all(
            state(services[s]) for s in ("main", "business"))
        if failure is not None:
            raise failure
        if failures or not receipt["quiescence_confirmed"]:
            raise RuntimeError("Container quiescence unverified")
        result = call(["cp", services["business"] + ":/state/.", str(destination / "state")], 45)
        receipt["steps"].append({"step": "copy_existing_state", "return_code": result.returncode})
        if result.returncode:
            raise RuntimeError("Private state capture failed")
        receipt["status"] = "passed"
    except BaseException as error:
        if failure is None or not isinstance(error, Exception):
            failure = error
        receipt.update(status="failed", error_type=type(error).__name__)
    finally:
        try:
            write_record(destination / "RECOVERY.json", receipt)
        except BaseException as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure
    return receipt


def require_capture(destination, task, *, deadline, docker):
    path = Path(destination) / "RECOVERY.json"
    value = json.loads(path.read_text())
    if (value.get("mode") != "stop_copy" or value.get("status") != "passed"
            or value.get("task") != str(Path(task).resolve())
            or value.get("quiescence_confirmed") is not True):
        raise RuntimeError("Existing cleanup is not a completed stop/copy capture")
    stops = [step for step in value["steps"] if step.get("step") == "stop"]
    if len(stops) != 2 or {step.get("service") for step in stops} != {"main", "business"}:
        raise RuntimeError("Cleanup lacks both owned containers")
    for step in stops:
        result = docker(["inspect", "--format", "{{.State.Running}}", step["container"]],
                        timeout=remaining(deadline, 20))
        if result.returncode or result.stdout.strip() != "false":
            raise RuntimeError("Previously stopped container is not quiescent")
    return value
