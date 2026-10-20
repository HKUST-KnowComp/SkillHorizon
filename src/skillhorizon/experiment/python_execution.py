"""Bounded Docker transport for a stopped-and-collected Python execution.

No Docker daemon, image, or resource support means unavailable, never host exec.
Only two trusted standalone helpers are mounted, never the research repository.
"""

import json
import math
import os
from pathlib import Path
import re
import selectors
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from .workspace_state import normalize_workspace_config


PROTOCOL = "python_execution_v1"
DOCKER = ("docker", "--host=unix:///var/run/docker.sock")
CAPABILITIES = ("CHOWN", "SETUID", "SETGID", "KILL", "DAC_READ_SEARCH")
_FIELDS = {"image", "max_calls", "timeout_seconds", "memory_bytes", "tmpfs_bytes",
           "pids_limit", "cpus", "max_code_bytes", "max_stream_bytes", "max_protocol_bytes"}
_RANGES = {"max_calls": (1, 1024), "timeout_seconds": (1, 20),
           "memory_bytes": (128 * 1024**2, 1024**3), "tmpfs_bytes": (8 * 1024**2, 256 * 1024**2),
           "pids_limit": (16, 128), "cpus": (1, 4), "max_code_bytes": (1, 131071),
           "max_stream_bytes": (128, 65536), "max_protocol_bytes": (1048576, 64 * 1024**2)}


class PythonExecutionError(RuntimeError):
    def __init__(self, code, *, execution_evidence=None):
        super().__init__(code)
        # Controller-only completed-process facts, never a usable file tree.
        self.execution_evidence = execution_evidence


def normalize_python_execution(config, workspace=None):
    if config is None:
        return None
    if not isinstance(config, dict) or set(config) != _FIELDS:
        raise ValueError("Python execution requires the exact declared resource fields")
    if not isinstance(config["image"], str) or re.fullmatch(r"sha256:[0-9a-f]{64}", config["image"]) is None:
        raise ValueError("Python image must be a complete immutable local image ID")
    for key, (low, high) in _RANGES.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise ValueError("Python resource outside supported bounded range: " + key)
    if config["tmpfs_bytes"] > config["memory_bytes"] // 2:
        raise ValueError("Python tmpfs must leave memory for processes and collection")
    if workspace is not None:
        workspace = normalize_workspace_config(workspace)
        frame = frame_bound(workspace, config)
        if frame > config["max_protocol_bytes"]:
            raise ValueError("Python protocol limit cannot hold a complete bounded snapshot")
        if workspace["max_total_bytes"] * 2 > config["tmpfs_bytes"]:
            raise ValueError("Python tmpfs cannot hold the declared workspace")
        if config["max_calls"] * frame * 2 > 1024**3:
            raise ValueError("Python execution journal would exceed the supported episode bound")
    return dict(config)


def frame_bound(workspace, config):
    # JSON escaping, base64 and maximum filesystem-component bytes are bounded.
    return (4 * ((workspace["max_total_bytes"] + 2) // 3)
            + workspace["max_entries"] * (workspace["max_depth"] * 255 * 6 + 128)
            + config["max_code_bytes"] * 6 + config["max_stream_bytes"] * 12 + 16384)


def python_entries():
    return [("python_execute", "Run bounded isolated Python on the logical workspace, without network or business APIs. "
             "Files persist as logical paths and bytes; POSIX permissions do not persist. Nonzero exit or timeout can leave file changes.",
             {"code": str})]


def python_tools():
    name, description, _ = python_entries()[0]
    return [{"type": "function", "name": name, "description": description,
             "parameters": {"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
             "strict": False}]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate result key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite result value")


def decode_frame(payload, config):
    if len(payload) > config["max_protocol_bytes"]:
        raise PythonExecutionError("EXECUTION_FRAME_TOO_LARGE")
    try:
        value = json.loads(payload.decode("utf-8"), object_pairs_hook=_object, parse_constant=_constant)
    except (ValueError, UnicodeError, RecursionError):
        raise PythonExecutionError("EXECUTION_FRAME_INVALID") from None
    if (not isinstance(value, dict) or set(value) != {"protocol", "valid", "tree", "result", "error_code"}
            or value["protocol"] != PROTOCOL or type(value["valid"]) is not bool):
        raise PythonExecutionError("EXECUTION_FRAME_INVALID")
    return value


def validate_public_result(result, config):
    fields = {"exit_code", "timed_out", "stdout", "stderr", "stdout_truncated", "stderr_truncated",
              "wall_seconds", "writers_cleared"}
    if (not isinstance(result, dict) or set(result) != fields or result["writers_cleared"] is not True
            or type(result["exit_code"]) is not int
            or any(type(result[k]) is not bool for k in ("timed_out", "stdout_truncated", "stderr_truncated"))
            or any(type(result[k]) is not str for k in ("stdout", "stderr"))
            or type(result["wall_seconds"]) not in (int, float)
            or not math.isfinite(result["wall_seconds"]) or result["wall_seconds"] < 0):
        raise PythonExecutionError("EXECUTION_RESULT_INVALID")
    for name in ("stdout", "stderr"):
        stream = result[name]
        # Each replacement character may represent one invalid raw byte. Every
        # other Unicode character needs its full UTF-8 length in the raw pipe.
        try:
            minimum_raw_bytes = sum(1 if c == "\ufffd" else len(c.encode("utf-8")) for c in stream)
        except UnicodeError:
            raise PythonExecutionError("EXECUTION_RESULT_INVALID") from None
        if minimum_raw_bytes > config["max_stream_bytes"]:
            raise PythonExecutionError("EXECUTION_RESULT_INVALID")
    return result


def validate_guest_result(value, before, workspace, config):
    from .workspace_state import validate_snapshot
    if value["valid"] is not True or value["error_code"] is not None:
        raise PythonExecutionError("EXECUTION_STATE_UNKNOWN")
    result = validate_public_result(value["result"], config)
    after = validate_snapshot(value["tree"], workspace)
    initial_inputs = {k: v for k, v in before.items() if k == "inputs" or k.startswith("inputs/")}
    if initial_inputs != {k: v for k, v in after.items() if k == "inputs" or k.startswith("inputs/")}:
        raise PythonExecutionError("EXECUTION_INPUTS_CHANGED")
    return after, result


def docker_arguments(config, name, helpers):
    args = ["create", "--name", name, "--interactive", "--read-only", "--network", "none", "--ipc", "none",
            "--user", "0:0", "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", str(config["pids_limit"]), "--cpus", str(config["cpus"]),
            "--memory", str(config["memory_bytes"]), "--memory-swap", str(config["memory_bytes"]),
            "--ulimit", "nofile=128:128", "--log-driver", "none",
            "--tmpfs", f'/workspace:rw,noexec,nosuid,nodev,size={config["tmpfs_bytes"]},mode=0755',
            "--tmpfs", "/tmp:rw,noexec,nosuid,nodev,size=8388608,mode=1777",
            "--mount", f"type=bind,src={helpers},dst=/harness,readonly",
            "--env", "PYTHONDONTWRITEBYTECODE=1", "--env", "PYTHONUNBUFFERED=1"]
    for capability in CAPABILITIES:
        args += ["--cap-add", capability]
    return [*args, config["image"], "python", "-I", "/harness/python_guest.py"]


def _exchange(argv, payload, limit, deadline):
    """Drain both streams and feed input without an unbounded communicate buffer."""
    if time.monotonic() >= deadline:
        raise PythonExecutionError("EXECUTION_TRANSPORT_TIMEOUT")
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    output = {"stdout": bytearray(), "stderr": bytearray()}
    sent = 0
    try:
        for stream, name, event in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                                    (process.stdout, "stdout", selectors.EVENT_READ),
                                    (process.stderr, "stderr", selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, event, name)
        while selector.get_map():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise PythonExecutionError("EXECUTION_TRANSPORT_TIMEOUT")
            for key, _ in selector.select(min(0.2, remaining)):
                stream, name = key.fileobj, key.data
                if name == "stdin":
                    try:
                        count = os.write(stream.fileno(), payload[sent:sent + 16384])
                    except BrokenPipeError:
                        count = 0
                        sent = len(payload)
                    sent += count
                    if sent >= len(payload):
                        selector.unregister(stream)
                        stream.close()
                else:
                    block = os.read(stream.fileno(), 16384)
                    if not block:
                        selector.unregister(stream)
                        stream.close()
                        continue
                    bound = limit if name == "stdout" else 65536
                    if len(output[name]) + len(block) > bound:
                        raise PythonExecutionError("EXECUTION_TRANSPORT_OUTPUT_LIMIT")
                    output[name].extend(block)
        code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
        return code, bytes(output["stdout"])
    finally:
        selector.close()
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=max(0.001, min(0.5, deadline - time.monotonic())))
        except subprocess.TimeoutExpired:
            raise PythonExecutionError("EXECUTION_CLIENT_CLEANUP_UNVERIFIED") from None
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()


class DockerPython:
    def __init__(self, config, workspace):
        self.config = normalize_python_execution(config, workspace)
        if self.config is None:
            raise ValueError("Python execution was not declared")
        self.workspace = normalize_workspace_config(workspace)

    def check_available(self):
        if sys.platform != "linux" or not shutil.which("docker"):
            raise PythonExecutionError("DOCKER_LINUX_BACKEND_UNAVAILABLE")
        result = subprocess.run([*DOCKER, "image", "inspect", "--format", "{{.Id}} {{.Os}}", self.config["image"]],
                                capture_output=True, timeout=10, check=False)
        if result.returncode or result.stdout.decode().strip() != self.config["image"] + " linux":
            raise PythonExecutionError("EXECUTION_IMAGE_UNAVAILABLE")

    def execute(self, before, code):
        from .workspace_state import validate_snapshot
        deadline = time.monotonic() + 60
        self.check_available()
        before = validate_snapshot(before, self.workspace)
        if not isinstance(code, str) or "\0" in code or len(code.encode("utf-8")) > self.config["max_code_bytes"]:
            raise ValueError("Code exceeds the declared UTF-8 byte limit")
        limits = {k: self.config[k] for k in ("timeout_seconds", "max_stream_bytes", "max_code_bytes", "max_protocol_bytes")}
        limits.update(startup_seconds=20, cleanup_seconds=3, capture_seconds=5)
        name = "skillhorizon-python-" + uuid.uuid4().hex
        with TemporaryDirectory(prefix="skillhorizon-python-helper-") as temporary:
            helpers = Path(temporary).resolve()
            for filename in ("workspace_state.py", "python_guest.py"):
                shutil.copyfile(Path(__file__).with_name(filename), helpers / filename)
            removed = False
            evidence = None
            try:
                created = subprocess.run([*DOCKER, *docker_arguments(self.config, name, helpers)],
                                         capture_output=True, timeout=20, check=False)
                if created.returncode or re.fullmatch(rb"[0-9a-f]{64}\s*", created.stdout) is None:
                    raise PythonExecutionError("EXECUTION_CONTAINER_CREATE_FAILED")
                now = time.monotonic()
                exchange_deadline = deadline - 10
                remaining = exchange_deadline - now
                # Reserve both removal checks, then admit only a guest schedule
                # whose startup, execution, cleanup and capture all fit.
                startup = min(20, remaining - limits["timeout_seconds"] - 3 - 5 - 1)
                if startup < 1:
                    raise PythonExecutionError("EXECUTION_DEADLINE_NOT_ADMISSIBLE")
                limits["startup_seconds"] = startup
                frame = json.dumps({"protocol": PROTOCOL, "code": code, "workspace_config": self.workspace,
                                    "tree": before, "limits": limits, "startup_deadline": now + startup}, ensure_ascii=True,
                                   separators=(",", ":")).encode()
                if len(frame) > self.config["max_protocol_bytes"]:
                    raise PythonExecutionError("EXECUTION_INPUT_FRAME_TOO_LARGE")
                status, output = _exchange([*DOCKER, "start", "--attach", "--interactive", name],
                                          frame, self.config["max_protocol_bytes"], exchange_deadline)
                if status != 0:
                    raise PythonExecutionError("EXECUTION_CONTAINER_FAILED")
                value = decode_frame(output, self.config)
                try:
                    public = validate_public_result(value["result"], self.config)
                    evidence = {"physical_executions": 1, "result": public}
                except PythonExecutionError:
                    pass
                after, result = validate_guest_result(value, before, self.workspace, self.config)
            except Exception as exc:
                code = str(exc) if isinstance(exc, PythonExecutionError) else "EXECUTION_BACKEND_FAILURE"
                raise PythonExecutionError(code, execution_evidence=evidence) from None
            finally:
                try:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PythonExecutionError("EXECUTION_CONTAINER_CLEANUP_UNVERIFIED", execution_evidence=evidence)
                    removal = subprocess.run([*DOCKER, "rm", "--force", name], capture_output=True,
                                             timeout=min(5, remaining), check=False)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise PythonExecutionError("EXECUTION_CONTAINER_CLEANUP_UNVERIFIED", execution_evidence=evidence)
                    check = subprocess.run([*DOCKER, "container", "inspect", name], capture_output=True,
                                           timeout=min(5, remaining), check=False)
                    removed = (removal.returncode == 0 and check.returncode != 0
                               and b"No such" in check.stderr)
                except (OSError, subprocess.TimeoutExpired):
                    removed = False
                if not removed:
                    raise PythonExecutionError("EXECUTION_CONTAINER_CLEANUP_UNVERIFIED", execution_evidence=evidence) from None
        if time.monotonic() > deadline:
            raise PythonExecutionError("EXECUTION_DEADLINE_EXCEEDED", execution_evidence=evidence)
        return {"tree": after, "result": result, "container_removed": True, "physical_executions": 1}
