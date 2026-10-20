"""Docker PTC transport; never execute guest code on the host.

The native owner must finish business-process cleanup before the shared parent
deadline. Fifteen seconds are reserved after exchange: ten for Docker removal
checks and five for native-owned business cleanup after an abnormal return.
"""

import os
import math
from pathlib import Path
import re
import selectors
import shutil
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
import uuid

from .python_execution import (DOCKER, PROTOCOL, DockerPython, PythonExecutionError,
                               docker_arguments, validate_guest_result, validate_public_result)
from .ptc_config import normalize_ptc_resources, TERMINAL_RESERVE_BYTES
from .ptc_protocol import IncrementalFrameDecoder, encode_frame
from .workspace_state import validate_snapshot


def _exchange_ptc(argv, initial, execution, bridge, deadline, *, before, workspace):
    config = bridge.budget.config
    state_bound = execution["max_protocol_bytes"] + TERMINAL_RESERVE_BYTES
    stream_bound = state_bound + config["max_bridge_bytes"] + TERMINAL_RESERVE_BYTES
    decoder = IncrementalFrameDecoder(max(state_bound, config["max_request_bytes"]),
                                      40, 1000000, stream_bound)
    if time.monotonic() >= deadline:
        raise PythonExecutionError("PTC_TRANSPORT_TIMEOUT")
    process = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    selector = selectors.DefaultSelector()
    outgoing = initial
    offset = 0
    header = bytearray()
    remaining_body = None
    current_size = 0
    bridge_bytes = 0
    terminal_bytes = 0
    final_bytes = 0
    stderr_bytes = 0
    final = None
    evidence = None
    input_registered = True
    input_closed = False
    pipe_broken = False
    outgoing_type = "initial"
    timeout_final = False
    bridge_finished = False
    response_written_bytes = 0

    def queue(frame):
        nonlocal outgoing, offset, input_registered, bridge_bytes, terminal_bytes, outgoing_type
        if outgoing:
            raise PythonExecutionError("PTC_DUPLEX_ORDER_INVALID")
        terminal = frame["type"] == "terminate"
        data = encode_frame(frame,
                            max_bytes=TERMINAL_RESERVE_BYTES if terminal else config["max_response_bytes"],
                            max_depth=config["max_json_depth"], max_nodes=config["max_json_nodes"])
        if not terminal:
            if bridge_bytes + len(data) > config["max_bridge_bytes"]:
                raise PythonExecutionError("PTC_WIRE_CAPACITY_EXCEEDED")
            bridge_bytes += len(data)
        else:
            terminal_bytes += len(data)
            if terminal_bytes > TERMINAL_RESERVE_BYTES:
                raise PythonExecutionError("PTC_TERMINAL_RESERVE_EXCEEDED")
        outgoing, offset = data, 0
        outgoing_type = frame["type"]
        if input_closed:
            if pipe_broken and outgoing_type == "child_response":
                return  # Retain the result; only a verified timeout final can resolve this race.
            raise PythonExecutionError("PTC_INPUT_ALREADY_CLOSED")
        if not input_registered:
            selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
            input_registered = True

    def receive_frame(frame, size):
        nonlocal bridge_bytes, final, input_closed, input_registered, final_bytes, evidence
        nonlocal outgoing, offset, timeout_final, bridge_finished
        if frame["type"] == "final":
            if final is not None or frame["seq"] != 0 or size > state_bound:
                raise PythonExecutionError("PTC_FINAL_FRAME_INVALID")
            value = frame["payload"]
            if (type(value) is not dict or set(value) != {"protocol", "valid", "tree", "result", "error_code"}
                    or value["protocol"] != PROTOCOL or type(value["valid"]) is not bool):
                raise PythonExecutionError("PTC_FINAL_FRAME_INVALID")
            try:
                public = validate_public_result(value["result"], execution)
                evidence = {"physical_executions": 1, "result": public}
            except (KeyError, TypeError, PythonExecutionError):
                pass
            timeout_final = (value["valid"] is True and evidence is not None
                             and evidence["result"]["timed_out"] is True
                             and evidence["result"]["writers_cleared"] is True)
            if timeout_final:
                # Validate the complete tree before reconciling effects. A bad
                # final must not authorize a softer business termination path.
                validate_guest_result(value, before, workspace, execution)
            if (outgoing or pipe_broken) and not timeout_final:
                raise PythonExecutionError("PTC_FINAL_FRAME_INVALID")
            if timeout_final:
                outgoing, offset = b"", 0
            else:
                bridge.finish()
                bridge_finished = True
            final, final_bytes = value, size
            if input_registered:
                selector.unregister(process.stdin)
                input_registered = False
            process.stdin.close()
            input_closed = True
            return
        if frame["type"] != "child_request" or size > config["max_request_bytes"] or final is not None:
            raise PythonExecutionError("PTC_CHILD_FRAME_INVALID")
        if outgoing:
            raise PythonExecutionError("PTC_DUPLEX_ORDER_INVALID")
        # Include actual incoming bytes, not merely a canonical re-encoding.
        bridge_bytes += size
        if bridge_bytes > config["max_bridge_bytes"]:
            raise PythonExecutionError("PTC_WIRE_CAPACITY_EXCEEDED")
        if bridge_bytes + config["max_response_bytes"] > config["max_bridge_bytes"]:
            bridge.budget.stop("WIRE_CAPACITY")
            queue({"type": "terminate", "seq": frame["seq"], "payload": {"code": "PTC_PROGRAM_LIMIT"}})
            return
        response = bridge.accept(frame)
        if response is not None:
            queue(response)

    try:
        for handle, name, event in ((process.stdin, "stdin", selectors.EVENT_WRITE),
                                    (process.stdout, "stdout", selectors.EVENT_READ),
                                    (process.stderr, "stderr", selectors.EVENT_READ)):
            os.set_blocking(handle.fileno(), False)
            selector.register(handle, event, name)
        while selector.get_map() or (final is not None and not bridge_finished):
            if time.monotonic() >= deadline:
                raise PythonExecutionError("PTC_TRANSPORT_TIMEOUT")
            if bridge.worker_seq is not None and not outgoing:
                response = bridge.poll(time.monotonic())
                if response is not None and not timeout_final:
                    queue(response)
            if timeout_final and not bridge_finished and bridge.worker_seq is None:
                bridge.finish("PROGRAM_TIMEOUT")
                bridge_finished = True
            for key, _ in selector.select(min(0.02, max(0, deadline - time.monotonic()))):
                handle, name = key.fileobj, key.data
                if handle.closed or (name == "stdin" and not input_registered):
                    continue
                if name == "stdin":
                    try:
                        count = os.write(handle.fileno(), outgoing[offset:offset + 16384])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        if outgoing_type != "child_response":
                            raise
                        selector.unregister(handle)
                        input_registered = False
                        input_closed = pipe_broken = True
                        handle.close()
                        continue
                    if outgoing_type == "child_response":
                        response_written_bytes += count
                    offset += count
                    if offset == len(outgoing):
                        selector.unregister(handle)
                        input_registered = False
                        outgoing, offset = b"", 0
                    continue
                count = 4 - len(header) if name == "stdout" and remaining_body is None else (
                    min(65536, remaining_body) if name == "stdout" else 16384)
                try:
                    block = os.read(handle.fileno(), count)
                except BlockingIOError:
                    continue
                if not block:
                    selector.unregister(handle)
                    if name == "stdout":
                        decoder.finish()
                        if final is None:
                            raise PythonExecutionError("PTC_FINAL_FRAME_MISSING")
                    handle.close()
                    continue
                if name == "stderr":
                    stderr_bytes += len(block)
                    if stderr_bytes > 65536:
                        raise PythonExecutionError("PTC_SUPERVISOR_STDERR_LIMIT")
                    continue
                if remaining_body is None:
                    header.extend(block)
                    frames = decoder.feed(block)
                    if len(header) == 4:
                        remaining_body = struct.unpack(">I", header)[0]
                        current_size = remaining_body + 4
                else:
                    remaining_body -= len(block)
                    frames = decoder.feed(block)
                if frames:
                    receive_frame(frames[0], current_size)
                    header.clear()
                    remaining_body = None
        status = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        if status or final is None or time.monotonic() >= deadline:
            raise PythonExecutionError("PTC_CONTAINER_FAILED")
        return final, {"bridge_wire_bytes": bridge_bytes + terminal_bytes,
                       "bridge_budgeted_wire_bytes": bridge_bytes,
                       "child_response_written_bytes": response_written_bytes,
                       "bridge_terminal_wire_bytes": terminal_bytes,
                       "initial_wire_bytes": len(initial), "final_wire_bytes": final_bytes}
    except BaseException as exc:
        try:
            bridge.abort("GUEST_TRANSPORT_UNKNOWN")
        finally:
            if isinstance(exc, Exception):
                code = str(exc) if isinstance(exc, PythonExecutionError) else "PTC_EXCHANGE_FAILED"
                raise PythonExecutionError(code, execution_evidence=evidence) from None
            raise
    finally:
        selector.close()
        try:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=max(0.001, min(0.5, deadline - time.monotonic())))
        except (OSError, subprocess.TimeoutExpired):
            raise PythonExecutionError("PTC_CLIENT_CLEANUP_UNVERIFIED", execution_evidence=evidence) from None
        finally:
            for handle in (process.stdin, process.stdout, process.stderr):
                handle.close()


class DockerPTC(DockerPython):
    def __init__(self, execution, workspace, ptc):
        super().__init__(execution, workspace)
        self.ptc = normalize_ptc_resources(ptc)

    def _check_available_before(self, deadline):
        # Reserve the full guest schedule and both Docker/native cleanup windows
        # even before image I/O; a late caller does not receive a fresh timeout.
        reserve = self.config["timeout_seconds"] + 1 + 3 + 5 + 1 + 15
        remaining = deadline - time.monotonic() - reserve
        if remaining <= 0:
            raise PythonExecutionError("PTC_DEADLINE_NOT_ADMISSIBLE")
        if sys.platform != "linux" or not shutil.which("docker"):
            raise PythonExecutionError("DOCKER_LINUX_BACKEND_UNAVAILABLE")
        result = subprocess.run([*DOCKER, "image", "inspect", "--format", "{{.Id}} {{.Os}}", self.config["image"]],
                                capture_output=True, timeout=min(10, remaining), check=False)
        if result.returncode or result.stdout.decode().strip() != self.config["image"] + " linux":
            raise PythonExecutionError("EXECUTION_IMAGE_UNAVAILABLE")
        if time.monotonic() >= deadline - reserve:
            raise PythonExecutionError("PTC_DEADLINE_NOT_ADMISSIBLE")

    def execute(self, before, code, *, bridge, deadline=None):
        started = time.monotonic()
        if deadline is not None and (type(deadline) not in (float, int) or not math.isfinite(deadline)
                                     or deadline <= started):
            raise ValueError("A finite future controller deadline is required")
        deadline = min(started + 60, deadline) if deadline is not None else started + 60
        if bridge.budget.config != self.ptc:
            raise ValueError("PTC backend and private grant resource declarations differ")
        try:
            self._check_available_before(deadline)
        except Exception as exc:
            bridge.abort("PTC_PREFLIGHT_FAILED")
            code = str(exc) if isinstance(exc, PythonExecutionError) else "PTC_PREFLIGHT_FAILED"
            raise PythonExecutionError(code) from None
        before = validate_snapshot(before, self.workspace)
        if type(code) is not str or "\0" in code or len(code.encode("utf-8")) > self.config["max_code_bytes"]:
            raise ValueError("Code exceeds the declared UTF-8 byte limit")
        limits = {k: self.config[k] for k in ("timeout_seconds", "max_stream_bytes", "max_code_bytes", "max_protocol_bytes")}
        limits.update(startup_seconds=20, cleanup_seconds=3, capture_seconds=5)
        name = "skillhorizon-ptc-" + uuid.uuid4().hex
        evidence = None
        with TemporaryDirectory(prefix="skillhorizon-ptc-helper-") as temporary:
            helpers = Path(temporary).resolve()
            for filename in ("workspace_state.py", "python_guest.py", "ptc_protocol.py", "ptc_guest_client.py",
                             "ptc_guest.py", "ptc_bootstrap.py"):
                shutil.copyfile(Path(__file__).with_name(filename), helpers / filename)
                (helpers / filename).chmod(0o444)
            helpers.chmod(0o755)
            try:
                args = docker_arguments(self.config, name, helpers)
                args[-1] = "/harness/ptc_guest.py"
                remaining = deadline - time.monotonic() - 15
                if remaining <= 0:
                    raise PythonExecutionError("PTC_DEADLINE_NOT_ADMISSIBLE")
                created = subprocess.run([*DOCKER, *args], capture_output=True,
                                         timeout=min(20, remaining), check=False)
                if created.returncode or re.fullmatch(rb"[0-9a-f]{64}\s*", created.stdout) is None:
                    raise PythonExecutionError("PTC_CONTAINER_CREATE_FAILED")
                now = time.monotonic()
                exchange_deadline = deadline - 15
                startup = min(20, exchange_deadline - now - limits["timeout_seconds"] - 3 - 5 - 1)
                if startup < 1:
                    raise PythonExecutionError("PTC_DEADLINE_NOT_ADMISSIBLE")
                limits["startup_seconds"] = startup
                request = {"protocol": PROTOCOL, "code": code, "workspace_config": self.workspace,
                           "tree": before, "limits": limits, "startup_deadline": now + startup}
                initial = encode_frame({"type": "initial", "seq": 0,
                                        "payload": {"execution": request, "ptc": self.ptc}},
                                       max_bytes=self.config["max_protocol_bytes"] + TERMINAL_RESERVE_BYTES,
                                       max_depth=40, max_nodes=1000000)
                value, wire_usage = _exchange_ptc([*DOCKER, "start", "--attach", "--interactive", name],
                                                   initial, self.config, bridge, exchange_deadline,
                                                   before=before, workspace=self.workspace)
                try:
                    public = validate_public_result(value["result"], self.config)
                    evidence = {"physical_executions": 1, "result": public}
                except (KeyError, TypeError, PythonExecutionError):
                    pass
                after, result = validate_guest_result(value, before, self.workspace, self.config)
            except Exception as exc:
                if isinstance(exc, PythonExecutionError) and exc.execution_evidence is not None:
                    evidence = exc.execution_evidence
                code = str(exc) if isinstance(exc, PythonExecutionError) else "PTC_BACKEND_FAILURE"
                try:
                    bridge.abort("PTC_BACKEND_FAILURE")
                finally:
                    raise PythonExecutionError(code, execution_evidence=evidence) from None
            finally:
                removed = False
                try:
                    remaining = deadline - 5 - time.monotonic()
                    if remaining > 0:
                        removal = subprocess.run([*DOCKER, "rm", "--force", name], capture_output=True,
                                                 timeout=min(5, remaining), check=False)
                        remaining = deadline - 5 - time.monotonic()
                        if remaining > 0:
                            check = subprocess.run([*DOCKER, "container", "inspect", name], capture_output=True,
                                                   timeout=min(5, remaining), check=False)
                            removed = removal.returncode == 0 and check.returncode != 0 and b"No such" in check.stderr
                except (OSError, subprocess.TimeoutExpired):
                    pass
                if not removed:
                    try:
                        bridge.abort("PTC_CONTAINER_CLEANUP_UNKNOWN")
                    finally:
                        raise PythonExecutionError("PTC_CONTAINER_CLEANUP_UNVERIFIED", execution_evidence=evidence)
        if time.monotonic() > deadline - 5:
            try:
                bridge.abort("PTC_NATIVE_CLEANUP_RESERVE_EXCEEDED")
            finally:
                raise PythonExecutionError("PTC_DEADLINE_EXCEEDED", execution_evidence=evidence)
        return {"tree": after, "result": result, "container_removed": True,
                "physical_executions": 1, **wire_usage}
