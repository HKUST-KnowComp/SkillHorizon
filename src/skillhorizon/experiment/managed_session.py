"""Native-owned business lifecycle; these files are never mounted for the agent."""

import asyncio
import math
from contextlib import contextmanager
import os
from pathlib import Path
import stat
import sys
import threading
import time
import uuid

from ..util import canonical_json, load_json
from .business_binding import business_binding
from .business_worker_process import ManagedBusinessProcess, WorkerClient
from .execution_bundle import atomic_json
from .ptc_budget import GrantStore, make_grant
from .ptc_protocol import bounded_json_bytes


CONTROL = "business_control.json"
STATE = "business_state.json"
WORKER_STATE = "business_worker_state.json"
READY = "business_ready.json"
PARENT = "ptc_parent.json"
RETURN = "business_call_state.json"
PROTOCOL = "managed_business_v1"
STATE_BYTES = 16 * 1024**2
STARTUP_TIMEOUT_SECONDS = 10
STARTUP_CLEANUP_SECONDS = 5
# Ready wait, constructor failure cleanup, and cancellation cleanup in the owner thread.
STARTUP_HANDOFF_SECONDS = STARTUP_TIMEOUT_SECONDS + 1 + STARTUP_CLEANUP_SECONDS


class PrivateStateReplaced(ValueError):
    """Atomic publication prevented a current private-state snapshot."""


def read_private(path):
    """Bound private controller data without reading model workspace files."""
    import json
    for _ in range(3):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_size > STATE_BYTES or info.st_nlink not in (0, 1)):
                raise ValueError("Invalid private business state file")
            # Atomic publish can unlink the inode between open and fstat.
            # Never consume that stale inode; reopen and validate the new file.
            if info.st_nlink == 0:
                continue
            data = handle.read(STATE_BYTES + 1)
            break
    else:
        raise PrivateStateReplaced("Private business state changed repeatedly while opening")
    if len(data) > STATE_BYTES:
        raise ValueError("Private business state is oversized")

    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Duplicate business state field")
            value[key] = item
        return value

    value = json.loads(data, object_pairs_hook=unique)
    bounded_json_bytes(value, max_bytes=STATE_BYTES, max_depth=40, max_nodes=262144)
    return value


def publish(path, value):
    bounded_json_bytes(value, max_bytes=STATE_BYTES, max_depth=40, max_nodes=262144)
    atomic_json(path, value)


def identity(value):
    return {key: value[key] for key in ("protocol", "bundle_id", "episode_id", "invocation_id")}


def matched(value, control):
    if identity(value) != identity(control) or value["protocol"] != PROTOCOL:
        raise ValueError("Business lifecycle identity differs")
    return value


def publish_worker_state(bundle, bundle_id, episode_id, invocation_id, phase, sequence):
    if phase not in {"starting", "idle", "busy", "failed", "closed"}:
        raise ValueError("Invalid worker phase")
    if type(sequence) is not int or sequence < 0:
        raise ValueError("Invalid worker sequence")
    publish(Path(bundle) / WORKER_STATE, {"protocol": PROTOCOL, "bundle_id": bundle_id,
        "episode_id": episode_id, "invocation_id": invocation_id,
        "worker_pid": os.getpid(), "phase": phase, "sequence": sequence})


def worker_idle(bundle, control, sequence):
    state = matched(read_private(Path(bundle) / WORKER_STATE), control)
    if (state.get("worker_pid") != control["worker_pid"] or state.get("phase") != "idle"
            or type(state.get("sequence")) is not int or state["sequence"] != sequence):
        raise ValueError("Business worker is not confirmed idle at this boundary")
    return state


def wait_worker_idle(bundle, control, sequence, deadline):
    while True:
        if time.monotonic() >= deadline:
            raise ValueError("Business worker completion is unverified")
        try:
            state = matched(read_private(Path(bundle) / WORKER_STATE), control)
        except (PrivateStateReplaced, FileNotFoundError):
            state = None
        if state is not None:
            if (state.get("worker_pid") != control["worker_pid"]
                    or type(state.get("sequence")) is not int or state["sequence"] < 0
                    or state.get("phase") not in {"starting", "idle", "busy", "failed", "closed"}):
                raise ValueError("Invalid business worker state at completion boundary")
            if state["phase"] in {"failed", "closed"}:
                raise ValueError("Business worker completion is unverified") from None
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError("Business worker completion is unverified")
        if state is not None and state["phase"] == "idle" and state["sequence"] == sequence:
            return state
        time.sleep(min(0.005, remaining))


def _lifecycle_outcome(operation, *args):
    # Keep even BaseException inside the owned future until cleanup has settled.
    try:
        return operation(*args), None
    except BaseException as exc:
        return None, exc


async def _settle_lifecycle(future, deadline, cancel):
    loop = asyncio.get_running_loop()
    cancelled = None
    while True:
        if future.done():
            value, error = future.result()
            return value, error, cancelled
        try:
            value, error = await asyncio.wait_for(asyncio.shield(future),
                                                 max(0, deadline - loop.time()))
            return value, error, cancelled
        except asyncio.CancelledError as exc:
            cancelled = cancelled or exc
            cancel()
        except TimeoutError as exc:
            return None, exc, cancelled


class ManagedEpisode:
    """The native controller retains the owner on every failure path."""

    def __init__(self, bundle, max_tool_calls):
        self.bundle = Path(bundle).resolve()
        self.binding = business_binding(self.bundle)
        if self.binding["business_transport"] != "managed_worker":
            raise ValueError("Managed business transport was not declared")
        if type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 100000:
            raise ValueError("Invalid controller tool budget")
        self.max_tool_calls = max_tool_calls
        self.owner = None
        self.control = {"protocol": PROTOCOL, "bundle_id": uuid.uuid4().hex,
                        "episode_id": uuid.uuid4().hex, "invocation_id": uuid.uuid4().hex,
                        "socket_path": None, "worker_pid": None}
        if (self.bundle / "business_replay.json").exists():
            replay = read_private(self.bundle / "business_replay.json")
            if set(replay) != {"bundle_id", "episode_id", "source_invocation_id", "mode"} or replay["mode"] not in {"full", "prefix"}:
                raise ValueError("Invalid business reconstruction identity")
            self.control.update({key: replay[key] for key in ("bundle_id", "episode_id")})
        self.phase = "starting"
        self.sequence = 0
        self.current = None
        self.deadline = None
        self.guest_quiescent = True
        self.workspace_index = None
        self.started = False
        self._lifecycle_lock = threading.Lock()
        self._close_requested = threading.Event()
        self._startup_future = None
        self._cleanup_future = None

    def start(self, environment):
        with self._lifecycle_lock:
            if self.started or self._close_requested.is_set():
                raise ValueError("Managed episode cannot start twice or after close")
            try:
                self._start(environment)
            finally:
                # A cancelled/expired async waiter cannot abandon a late owner.
                if self._close_requested.is_set():
                    self._close_locked(time.monotonic() + STARTUP_CLEANUP_SECONDS)

    def _start(self, environment):
        if any((self.bundle / name).exists() for name in (CONTROL, STATE, WORKER_STATE, READY, PARENT, RETURN)):
            raise ValueError("Managed episode requires fresh private state")
        self.started = True
        publish(self.bundle / CONTROL, self.control)
        self._publish()
        command = [sys.executable, "-B", "-m", "skillhorizon.experiment.business_worker_server", str(self.bundle),
                   "--bundle-id", self.control["bundle_id"], "--episode-id", self.control["episode_id"],
                   "--invocation-id", self.control["invocation_id"]]
        try:
            self.owner = ManagedBusinessProcess(command, env=environment,
                                                startup_timeout=STARTUP_TIMEOUT_SECONDS)
            self.control.update(socket_path=self.owner.socketpath, worker_pid=self.owner.process.pid)
            publish(self.bundle / CONTROL, self.control)
        except BaseException as exc:
            self.owner = getattr(exc, "owner", None) or self.owner
            self.phase = "failed"
            self._publish()
            raise

    async def start_async(self, environment):
        """Bound the waiter without abandoning the thread's ownership obligation.

        Popen/filesystem stalls are not bounded by the worker's ready timeout.
        On handoff expiry the retained start thread closes its eventual owner;
        close() cannot certify a stopped worker while that handoff is pending.
        Repeated cancellation never resets either wait deadline.
        """
        if self._startup_future is not None or self.started or self._close_requested.is_set():
            raise ValueError("Managed episode startup has already been claimed")
        loop = asyncio.get_running_loop()
        self._startup_future = loop.run_in_executor(None, _lifecycle_outcome, self.start, environment)
        _, failure, cancelled = await _settle_lifecycle(
            self._startup_future, loop.time() + STARTUP_HANDOFF_SECONDS, self._close_requested.set)
        if failure is None and cancelled is None:
            return
        _, cleanup_error, cleanup_cancelled = await self._close_async_result(
            time.monotonic() + STARTUP_CLEANUP_SECONDS)
        cancelled = cancelled or cleanup_cancelled
        if cancelled is not None:
            raise cancelled from (cleanup_error or failure)
        if cleanup_error is not None:
            raise cleanup_error from failure
        raise failure

    async def _close_async_result(self, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Invalid managed cleanup deadline")
        self._close_requested.set()
        loop = asyncio.get_running_loop()
        wait_deadline = loop.time() + max(0, deadline - time.monotonic())
        self._cleanup_future = loop.run_in_executor(None, _lifecycle_outcome, self.close, deadline)
        result, error, cancelled = await _settle_lifecycle(
            self._cleanup_future, wait_deadline, self._close_requested.set)
        if error is None and result is not None and not result:
            error = RuntimeError("Managed cleanup is unverified")
        return result, error, cancelled

    async def close_async(self, deadline):
        """Settle cleanup under the original deadline before re-raising cancellation.

        A timeout never cancels the retained cleanup future. If startup is still
        in flight, its own thread also retains the late-owner cleanup obligation.
        """
        result, error, cancelled = await self._close_async_result(deadline)
        if cancelled is not None:
            raise cancelled from error
        if error is not None:
            raise error
        return result

    def _publish(self):
        publish(self.bundle / STATE, {**identity(self.control), "phase": self.phase,
            "worker_sequence": self.sequence, "active_call_id": self.current and self.current["call_id"],
            "worker_stopped": bool(self.owner and self.owner.quiescent),
            "guest_quiescent": self.guest_quiescent})

    def accept_ready(self):
        ready = matched(read_private(self.bundle / READY), self.control)
        if ready.get("tools") != load_json(self.bundle / "private_task.json")["tools"]:
            raise ValueError("Managed MCP business catalog differs")
        if self.owner.process.poll() is not None:
            raise ValueError("Managed worker exited before admission")
        worker_idle(self.bundle, self.control, 0)
        self.phase = "idle"
        self._publish()

    def guard(self):
        if self.phase != "idle" or self.current is not None or self.owner.process.poll() is not None:
            raise ValueError("Managed episode is not at an idle controller boundary")
        worker_idle(self.bundle, self.control, self.sequence)

    def begin_parent(self, event, *, prior_nested, workspace_index, deadline, reconstruct=False):
        self.guard()
        if (type(deadline) not in (int, float) or not math.isfinite(deadline)
                or deadline <= time.monotonic() or type(reconstruct) is not bool):
            raise ValueError("Invalid native parent deadline or mode")
        parent = {**identity(self.control), **event, "workspace_index": workspace_index,
                  "deadline_monotonic": deadline, "reconstruct": reconstruct, "grant": None}
        if event["name"] == "ptc_execute" and not reconstruct:
            grant = make_grant(bundle_id=self.control["bundle_id"], episode_id=self.control["episode_id"],
                parent_id=event["call_id"], top_level_index=event["tool_index"], prior_nested=prior_nested,
                max_tool_calls=self.max_tool_calls, config=self.binding["ptc"], deadline_monotonic=deadline)
            GrantStore(self.bundle / "ptc_grants", self.binding["ptc"]).issue(grant)
            parent["grant"] = grant
        self.current, self.deadline, self.phase = event, deadline, "active"
        self.workspace_index = workspace_index
        if event["name"] in {"python_execute", "ptc_execute"} and not reconstruct:
            self.guest_quiescent = False
        publish(self.bundle / PARENT, parent)
        self._publish()

    def finish_parent(self):
        if self.phase != "active" or self.current is None:
            raise ValueError("No active native parent")
        returned = matched(read_private(self.bundle / RETURN), self.control)
        if (returned.get("phase") != "complete" or returned.get("call_id") != self.current["call_id"]
                or returned.get("tool_index") != self.current["tool_index"]
                or type(returned.get("worker_sequence")) is not int
                or returned["worker_sequence"] < self.sequence or time.monotonic() >= self.deadline - 5):
            raise ValueError("Parent completion is unverified or too late")
        self.sequence = returned["worker_sequence"]
        if self.owner.process.poll() is not None:
            raise ValueError("Business worker exited after the parent response")
        worker_idle(self.bundle, self.control, self.sequence)
        self.guest_quiescent = True
        self.current, self.deadline, self.phase = None, None, "idle"
        self._publish()

    def observe_guest_cleanup(self):
        """A failed file commit may still have a validated, cleared guest."""
        if self.guest_quiescent or self.current is None:
            return
        try:
            receipt = read_private(self.bundle / "workspace_actions.jsonl.execution_receipt")
            action, usage = receipt["action"], receipt["usage"]
            if (usage["actions"] != self.workspace_index or action["name"] != self.current["name"]
                    or canonical_json(action["arguments"]) != canonical_json(self.current["arguments"])):
                return
            from .python_execution import validate_public_result
            from .execution_bundle import execution_config
            public = validate_public_result(action["output"]["data"], execution_config(self.bundle))
            self.guest_quiescent = public["writers_cleared"] is True
        except Exception:
            return

    @contextmanager
    def verification(self):
        self.guard()
        self.phase = "verification"
        self._publish()
        try:
            yield
        except BaseException:
            self.phase = "failed"
            self._publish()
            raise
        else:
            self.phase = "idle"
            self._publish()

    def close(self, deadline):
        if type(deadline) not in (int, float) or not math.isfinite(deadline):
            raise ValueError("Invalid managed cleanup deadline")
        self._close_requested.set()
        if not self._lifecycle_lock.acquire(timeout=max(0, deadline - time.monotonic())):
            raise TimeoutError("Managed startup ownership handoff is still pending")
        try:
            return self._close_locked(deadline)
        finally:
            self._lifecycle_lock.release()

    def _close_locked(self, deadline):
        if not self.started:
            return None
        self.observe_guest_cleanup()
        self.phase = "closing"
        try:
            self._publish()
        finally:
            result = self.owner.close(deadline) if self.owner is not None else None
            self.phase = "stopped" if self.owner is None or bool(result) else "quarantined"
            self.current = None
            self._publish()
        return result


def require_world_quiescent(bundle):
    """External readers require stopped writers; prefix checks use a frozen phase."""
    bundle = Path(bundle)
    if not (bundle / "manifest.json").exists():
        return
    if business_binding(bundle)["business_transport"] != "managed_worker":
        return
    control = read_private(bundle / CONTROL)
    state = matched(read_private(bundle / STATE), control)
    if state.get("guest_quiescent") is not True:
        raise ValueError("Guest quiescence is unverified")
    if state.get("phase") == "stopped" and state.get("worker_stopped") is True:
        return
    if state.get("phase") == "verification" and state.get("active_call_id") is None:
        worker_idle(bundle, control, state["worker_sequence"])
        return
    raise ValueError("Mutable business world is not frozen for observation")


class ManagedBusiness:
    """One MCP-owned client, shared by direct business calls and PTC."""

    def __init__(self, bundle):
        self.bundle = Path(bundle)
        self.control = read_private(self.bundle / CONTROL)
        if business_binding(bundle)["business_transport"] != "managed_worker":
            raise ValueError("Managed business transport was not declared")
        self.client = WorkerClient(self.control["socket_path"])
        try:
            tools = self.client.wait_ready(time.monotonic() + 10)
            if tools != load_json(self.bundle / "private_task.json")["tools"]:
                raise ValueError("Managed worker exported catalog differs")
            wait_worker_idle(self.bundle, self.control, 0, time.monotonic() + 5)
        except BaseException:
            self.client.close()
            raise
        self.metadata = {tool["name"]: {"description": tool["description"], "input_schema": tool["parameters"]}
                         for tool in tools}
        publish(self.bundle / READY, {**identity(self.control), "tools": tools})

    def call(self, name, arguments):
        from .execution_bundle import ensure_execution_known
        ensure_execution_known(self.bundle)
        parent = matched(read_private(self.bundle / PARENT), self.control)
        state = matched(read_private(self.bundle / STATE), self.control)
        if (state.get("phase") != "active" or state.get("active_call_id") != parent["call_id"]
                or parent["name"] != name or canonical_json(parent["arguments"]) != canonical_json(arguments)):
            raise ValueError("Business call differs from native active parent")
        return self.client.call(name, arguments, deadline=parent["deadline_monotonic"] - 15)


class ManagedMCPRuntime:
    """The selected MCP entry owns one client and publishes parent completion."""

    def __init__(self, bundle, business, workspace):
        self.bundle, self.business, self.workspace = Path(bundle), business, workspace
        from .workspace_bundle import WorkspaceComposition
        self.composed = WorkspaceComposition(business, workspace) if workspace is not None else business
        self.metadata = self.composed.metadata
        self.workspace_names = getattr(self.composed, "workspace_names", set())
        from .internal_mcp import FORMAT
        self.internal = load_json(self.bundle / "private_task.json")["format"] == FORMAT

    def is_error(self, name, result):
        return result["status"] != "success" if self.internal and name not in self.workspace_names else not result["ok"]

    def call(self, name, arguments):
        from .execution_bundle import ensure_execution_known
        ensure_execution_known(self.bundle)
        parent = matched(read_private(self.bundle / PARENT), self.business.control)
        state = matched(read_private(self.bundle / STATE), self.business.control)
        if (state.get("phase") != "active" or state.get("active_call_id") != parent["call_id"]
                or name != parent["name"] or canonical_json(arguments) != canonical_json(parent["arguments"])
                or time.monotonic() >= parent["deadline_monotonic"] - 15):
            raise ValueError("MCP request lacks its current native parent")
        receipt = {**identity(self.business.control), "call_id": parent["call_id"],
                   "tool_index": parent["tool_index"], "phase": "failed", "worker_sequence": None}
        try:
            if name == "ptc_execute":
                output = self.workspace.call(name, arguments, ptc_parent=parent)
            else:
                output = self.composed.call(name, arguments)
            sequence = self.business.client.last_reply.seq if self.business.client.last_reply is not None else 0
            wait_worker_idle(self.bundle, self.business.control, sequence, parent["deadline_monotonic"] - 10)
            ensure_execution_known(self.bundle)
            receipt.update(phase="complete", worker_sequence=sequence)
            publish(self.bundle / RETURN, receipt)
            return output
        except BaseException:
            try:
                publish(self.bundle / RETURN, receipt)
            except Exception:
                pass
            raise


async def serve_managed(bundle, runtime):
    bundle = Path(bundle).resolve()
    if Path(runtime).resolve() != bundle / "runtime" or not (bundle / "runtime").is_dir():
        raise ValueError("Managed MCP must use its native-owned existing world")
    business = ManagedBusiness(bundle)
    try:
        from .workspace_bundle import start_workspace
        from integrations.internal_mcp.transport import serve
        workspace = start_workspace(bundle, business=business)
        await serve(ManagedMCPRuntime(bundle, business, workspace), "skillhorizon-managed")
    finally:
        business.client.close()
