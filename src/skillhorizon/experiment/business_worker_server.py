"""Trusted serial business runtime, owned and terminated by the native runner.

This is not an agent or a model-code executor. Only the existing bundle's
business runtime is instantiated, and its actual exported schema is checked.
The Unix endpoint is controller-private and is never mounted into a guest.
"""

import argparse
import asyncio
from copy import deepcopy
import os
import re
from pathlib import Path
import shutil
import socket
import stat
import sys

from ..host_runtime import require_supported_python
from ..util import canonical_json, load_json
from .argument_feedback import schema_rejection
from .ptc_protocol import IncrementalFrameDecoder, encode_frame, ProtocolError


PROTOCOL = "business_worker_v1"
MAX_FRAME_BYTES = 16 * 1024**2
MAX_TOTAL_BYTES = 1024**3
MAX_JSON_DEPTH = 32
MAX_JSON_NODES = 262144


class BusinessWorkerError(RuntimeError):
    pass


async def _ops_tools(runtime, task):
    from mcp import Client
    from mcp.server import MCPServer
    from ..public_contract import register_tools
    from .mcp_tools import provider_tools_from_mcp_listing
    server = MCPServer("SkillHorizon Business Metadata")
    register_tools(server, runtime, runtime.tool_entries(task["execution_mode"]))
    async with Client(server) as client:
        return provider_tools_from_mcp_listing(await client.list_tools())


def load_business(bundle):
    """Same startup state and runtime classes as the two existing MCP entries."""
    require_supported_python()
    bundle = Path(bundle).resolve()
    from .execution_bundle import ensure_execution_known
    ensure_execution_known(bundle)
    task = load_json(bundle / "private_task.json")
    runtime_root = bundle / "runtime"
    if os.path.lexists(runtime_root):
        raise BusinessWorkerError("BUSINESS_RUNTIME_MUST_BE_FRESH")
    shutil.copytree(bundle / "checkpoint", runtime_root)
    from .internal_mcp import FORMAT as INTERNAL_FORMAT
    if task["format"] == INTERNAL_FORMAT:
        # The module uses the same RecordedRuntime as the inline MCP server.
        from integrations.internal_mcp.framework import RecordedRuntime, runtime_for, metadata_tools
        runtime = RecordedRuntime(runtime_for(task, bundle / "checkpoint/environment", runtime_root), runtime_root)
        tools = metadata_tools(runtime)
    else:
        from .project_runtime import project_runtime
        runtime = project_runtime(task, runtime_root)
        tools = asyncio.run(_ops_tools(runtime, task))
    if canonical_json(tools) != canonical_json(task["tools"]):
        raise BusinessWorkerError("BUSINESS_EXPORTED_SCHEMA_MISMATCH")
    return runtime, tools


def require_no_background_writers():
    """A serial Linux worker must be the only member of its owned group.

    Original fixed scripts are synchronous and remain in this process group.
    This is a fail-closed check, not confinement of untrusted host code: model
    programs live in the separate Docker namespace, never in this worker.
    """
    if sys.platform != "linux":
        raise BusinessWorkerError("BUSINESS_QUIESCENCE_REQUIRES_LINUX")
    own_pid, own_group = os.getpid(), os.getpgrp()
    if own_pid != own_group:
        raise BusinessWorkerError("BUSINESS_GROUP_NOT_OWNED")
    with os.scandir("/proc") as entries:
        for index, entry in enumerate(entries):
            if index > 100000:
                raise BusinessWorkerError("BUSINESS_PROCESS_SCAN_LIMIT")
            if not entry.name.isdecimal():
                continue
            try:
                with open(Path(entry.path) / "stat", "rb") as handle:
                    value = handle.read(8193)
            except (FileNotFoundError, ProcessLookupError):
                # A proc entry can disappear after scandir/open but before read.
                continue
            except OSError as exc:
                raise BusinessWorkerError("BUSINESS_PROCESS_SCAN_UNKNOWN") from exc
            if len(value) > 8192:
                raise BusinessWorkerError("BUSINESS_PROCESS_SCAN_UNKNOWN")
            try:
                fields = value[value.rindex(b")") + 2:].split()
                group = int(fields[2])
                pid = int(entry.name)
            except (ValueError, IndexError) as exc:
                raise BusinessWorkerError("BUSINESS_PROCESS_SCAN_UNKNOWN") from exc
            if group == own_group and pid != own_pid:
                raise BusinessWorkerError("BUSINESS_BACKGROUND_WRITER_PRESENT")


class BusinessDispatcher:
    def __init__(self, runtime, tools, *, quiescence=require_no_background_writers):
        from jsonschema import Draft202012Validator
        if type(tools) is not list or not tools:
            raise BusinessWorkerError("BUSINESS_CATALOG_INVALID")
        self.runtime = runtime
        self.tools = deepcopy(tools)
        self.validators = {}
        self.quiescence = quiescence
        for tool in tools:
            if (type(tool) is not dict or type(tool.get("name")) is not str
                    or tool["name"] in self.validators
                    or tool["name"] in {"ptc_execute", "python_execute"}
                    or tool["name"].startswith("workspace_")):
                raise BusinessWorkerError("BUSINESS_CATALOG_INVALID")
            Draft202012Validator.check_schema(tool["parameters"])
            self.validators[tool["name"]] = Draft202012Validator(tool["parameters"])
        self.failed = False

    def assert_quiescent(self):
        if self.failed:
            raise BusinessWorkerError("BUSINESS_WORKER_LOCKED")
        try:
            self.quiescence()
        except BaseException:
            self.failed = True
            raise

    def dispatch(self, request):
        self.assert_quiescent()
        if (type(request) is not dict or set(request) != {"name", "arguments"}
                or type(request["name"]) is not str):
            return {"dispatched": False, "output": {"ok": False, "error": {"code": "ARGUMENT_SCHEMA"}}}
        name, arguments = request["name"], request["arguments"]
        if name not in self.validators:
            return {"dispatched": False, "output": {"ok": False, "error": {"code": "TOOL_NOT_EXPORTED"}}}
        rejected = schema_rejection(self.validators[name], arguments)
        if rejected is not None:
            return {"dispatched": False, "output": rejected}
        try:
            output = self.runtime.call(name, deepcopy(arguments))
            self.assert_quiescent()
            if type(output) is not dict:
                raise BusinessWorkerError("BUSINESS_OUTPUT_INVALID")
            return {"dispatched": True, "output": output}
        except BaseException:
            self.failed = True
            raise


def _frame(kind, seq, payload):
    return encode_frame({"type": kind, "seq": seq, "payload": payload},
                        max_bytes=MAX_FRAME_BYTES, max_depth=MAX_JSON_DEPTH, max_nodes=MAX_JSON_NODES)


def failure_details(error, sequence):
    """Private bounded diagnostics, without runtime exception text or tool data."""
    code = str(error) if isinstance(error, BusinessWorkerError) else "BUSINESS_WORKER_EXCEPTION"
    if re.fullmatch(r"BUSINESS_[A-Z_]{1,80}", code) is None:
        code = "BUSINESS_WORKER_EXCEPTION"
    cause = error.__cause__
    return {"sequence": sequence, "code": code,
            "exception_type": type(error).__name__[:80],
            "cause_type": type(cause).__name__[:80] if cause is not None else None,
            "cause_errno": cause.errno if isinstance(cause, OSError) and type(cause.errno) is int else None}


def serve(socket_path, dispatcher, *, phase=None, failure=None):
    """One MCP peer, one outstanding call, no automatic retry or reconnection."""
    path = Path(socket_path)
    phase = phase if phase is not None else lambda state, sequence: None
    info = path.parent.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077
            or os.path.lexists(path) or len(os.fsencode(path)) >= 104):
        raise BusinessWorkerError("BUSINESS_SOCKET_NOT_PRIVATE")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(path))
        path.chmod(0o600)
        server.listen(1)
        connection, _ = server.accept()
        with connection:
            connection.settimeout(10)
            # No subprocess or socket is passed into the business runtime.
            handshake = _frame("initial", 0, {"protocol": PROTOCOL, "tools": dispatcher.tools})
            dispatcher.assert_quiescent()
            connection.sendall(handshake)
            dispatcher.assert_quiescent()
            phase("idle", 0)
            sent = len(handshake)
            decoder = IncrementalFrameDecoder(MAX_FRAME_BYTES, MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_TOTAL_BYTES)
            sequence = 1
            try:
                while True:
                    # Provider thinking is not a business-call timeout. The
                    # native owner can always terminate an idle worker.
                    connection.settimeout(None)
                    data = connection.recv(65536)
                    if not data:
                        decoder.finish()
                        phase("closed", sequence - 1)
                        return
                    frames = decoder.feed(data)
                    for frame in frames:
                        if frame["type"] != "child_request" or frame["seq"] != sequence:
                            raise BusinessWorkerError("BUSINESS_REQUEST_ORDER_INVALID")
                        # Reserve a full response before any business mutation.
                        if sent + decoder.total_bytes + MAX_FRAME_BYTES > MAX_TOTAL_BYTES:
                            raise BusinessWorkerError("BUSINESS_TRANSPORT_CAPACITY")
                        phase("busy", sequence)
                        result = dispatcher.dispatch(frame["payload"])
                        response = _frame("child_response", sequence, result)
                        dispatcher.assert_quiescent()
                        connection.settimeout(10)
                        connection.sendall(response)
                        # This cannot retract an observation already sent. A
                        # post-emit failure instead locks the episode and exits
                        # before accepting any further business request.
                        dispatcher.assert_quiescent()
                        phase("idle", sequence)
                        sent += len(response)
                        sequence += 1
            except Exception as exc:
                dispatcher.failed = True
                if failure is not None:
                    try:
                        failure(failure_details(exc, sequence))
                    except Exception:
                        pass
                try:
                    phase("failed", sequence)
                except Exception:
                    pass
                # A lost/oversize result is not a business rejection. Do not
                # echo exception text or attempt the business operation again.
                try:
                    connection.settimeout(1)
                    connection.sendall(_frame("terminate", sequence, {"code": "BUSINESS_EFFECT_UNKNOWN"}))
                except (OSError, ProtocolError):
                    pass
                raise


def main():
    require_supported_python()
    parser = argparse.ArgumentParser(description="Controller-owned SkillHorizon business worker")
    parser.add_argument("bundle", type=Path)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--bundle-id")
    parser.add_argument("--episode-id")
    parser.add_argument("--invocation-id")
    args = parser.parse_args()
    if sys.platform != "linux" or os.getpid() != os.getpgrp():
        raise BusinessWorkerError("BUSINESS_WORKER_REQUIRES_OWNED_LINUX_GROUP")
    if any(value is None for value in (args.bundle_id, args.episode_id, args.invocation_id)) and not all(
            value is None for value in (args.bundle_id, args.episode_id, args.invocation_id)):
        raise BusinessWorkerError("INCOMPLETE_BUSINESS_IDENTITY")
    phase = failure = None
    if args.bundle_id is not None:
        from .managed_session import publish_worker_state, read_private, CONTROL
        control = read_private(args.bundle / CONTROL)
        if (control["bundle_id"] != args.bundle_id or control["episode_id"] != args.episode_id
                or control["invocation_id"] != args.invocation_id):
            raise BusinessWorkerError("BUSINESS_IDENTITY_MISMATCH")
        phase = lambda state, sequence: publish_worker_state(args.bundle, args.bundle_id, args.episode_id,
                                                             args.invocation_id, state, sequence)
        from .managed_session import publish, identity
        failure = lambda details: publish(args.bundle / "business_worker_failure.json", {**identity(control), **details})
        phase("starting", 0)
    runtime, tools = load_business(args.bundle)
    require_no_background_writers()
    serve(args.socket, BusinessDispatcher(runtime, tools), phase=phase, failure=failure)


if __name__ == "__main__":
    main()
