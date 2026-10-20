"""Controller-owned POSIX business process and bounded, single-flight client.

Commands and environment values are frozen controller inputs, never guest/model
inputs. This module is not a sandbox or qualification of a domain's descendants:
production must separately certify that fixed scripts never escape their group.
All deadlines use time.monotonic(). A successful response is the trusted server's
save/record/no-background-writer confirmation, not proof supplied by the owner.
"""

from dataclasses import dataclass
import errno
import math
import os
from pathlib import Path
import re
import selectors
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import time

from .ptc_protocol import IncrementalFrameDecoder, ProtocolError, encode_frame


PROTOCOL = "business_worker_v1"
MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 262144
ENV_ALLOWLIST = frozenset({"PATH", "PYTHONPATH", "PYTHONNOUSERSITE",
                           "PYTHONDONTWRITEBYTECODE", "SYSTEMROOT"})


def _deadline(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("INVALID_DEADLINE")
    return value


class WorkerError(RuntimeError):
    """Payload-free transport error. Unknown dispatch must never be retried."""

    def __init__(self, code, *, owner=None):
        self.code = code
        # Startup failure must not discard the only process owner.
        self.owner = owner
        super().__init__(code)


@dataclass(frozen=True)
class CleanupResult:
    leader_reaped: bool
    group_gone: bool
    directory_removed: bool
    errors: tuple[str, ...] = ()

    def __bool__(self):
        return self.leader_reaped and self.group_gone and self.directory_removed and not self.errors


@dataclass(frozen=True)
class WorkerReply:
    seq: int
    dispatched: bool
    output: object


class ManagedBusinessProcess:
    """Native retains this object even after MCP failure or cleanup timeout.

    allow_mock enables POSIX component tests off Linux, not production eligibility.
    No ambient environment is copied. A failed close retains both the Popen handle
    and the private endpoint directory, including when this object is collected.
    """

    def __init__(self, command, *, env, startup_timeout=10, allow_mock=False):
        if type(allow_mock) is not bool:
            raise ValueError("INVALID_MOCK_OPTION")
        if os.name != "posix" or (sys.platform != "linux" and not allow_mock):
            raise WorkerError("UNSUPPORTED_PLATFORM")
        if sys.platform == "linux" and not Path("/proc/self/stat").is_file():
            raise WorkerError("PROC_UNAVAILABLE")
        if type(command) not in (list, tuple) or not command or any(
            type(arg) is not str or not arg or "\0" in arg for arg in command
        ) or not os.path.isabs(command[0]):
            raise ValueError("INVALID_TRUSTED_COMMAND")
        if type(env) is not dict or any(
            type(key) is not str or key not in ENV_ALLOWLIST or
            type(value) is not str or "\0" in value for key, value in env.items()
        ):
            raise ValueError("UNSAFE_ENVIRONMENT")
        _deadline(startup_timeout)
        if startup_timeout <= 0:
            raise ValueError("INVALID_STARTUP_TIMEOUT")
        clean_env = dict(env)
        clean_env.update(PYTHONNOUSERSITE="1", PYTHONDONTWRITEBYTECODE="1")
        self._directory = tempfile.TemporaryDirectory(prefix="sh-bw-", dir="/tmp")
        # TemporaryDirectory's GC finalizer would erase evidence on failed cleanup.
        self._directory._finalizer.detach()
        self.directory = self._directory.name
        os.chmod(self.directory, 0o700)
        self.socketpath = os.path.join(self.directory, "worker.sock")
        self.process = None
        self.pgid = None
        self.last_cleanup = None
        self._removed = False
        self._stopped = False
        try:
            self.process = subprocess.Popen(
                [*command, "--socket", self.socketpath], env=clean_env,
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True,
            )
            self.pgid = self.process.pid
            self.wait_ready(time.monotonic() + startup_timeout)
        except BaseException as error:
            # Construction has not returned, so the caller may have no variable
            # bound to this owner yet. Preserve it even for cancellation or a
            # second interruption during bounded cleanup.
            error.owner = self
            try:
                self.close(time.monotonic() + min(startup_timeout, 1.0))
            except BaseException as cleanup_error:
                cleanup_error.owner = self
                if not isinstance(error, Exception):
                    error.cleanup_error_type = type(cleanup_error).__name__
                    raise error
                raise
            if not isinstance(error, Exception):
                raise
            code = error.code if isinstance(error, WorkerError) else "WORKER_START_FAILED"
            raise WorkerError(code, owner=self) from None

    def wait_ready(self, deadline):
        """Only inspect the endpoint; the server accepts one long-lived client."""
        _deadline(deadline)
        while True:
            if self.process.poll() is not None:
                raise WorkerError("WORKER_EXITED", owner=self)
            try:
                mode = os.lstat(self.socketpath).st_mode
            except FileNotFoundError:
                pass
            else:
                if not stat.S_ISSOCK(mode):
                    raise WorkerError("INVALID_WORKER_SOCKET", owner=self)
                return self.socketpath
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise WorkerError("WORKER_START_TIMEOUT", owner=self)
            time.sleep(min(0.01, remaining))

    def _group_gone(self):
        if self._stopped or self.pgid is None:
            return True
        try:
            os.killpg(self.pgid, 0)
        except ProcessLookupError:
            return True
        return False

    @property
    def quiescent(self):
        """Forced-stop proof only; a live idle worker is conservatively False."""
        if self._stopped:
            return True
        leader_reaped = self.process is None or self.process.poll() is not None
        try:
            self._stopped = leader_reaped and self._group_gone()
            return self._stopped
        except OSError:
            return False

    def close(self, deadline):
        """TERM, then KILL, bounded by an absolute deadline; safe to retry.

        Errors are bounded errno codes in the result, never silently successful.
        A remaining zombie/group also prevents cleanup confirmation. No world
        access, scoring, or automatic episode recovery is authorized by this API.
        """
        _deadline(deadline)
        errors = []

        def error_code(operation, error):
            code = operation + "_" + str(error.errno)
            if code not in errors:
                errors.append(code)

        def gone():
            try:
                return self._group_gone()
            except OSError as error:
                error_code("GROUP_PROBE", error)
                return False

        def send(sig):
            if self.pgid is None:
                return
            try:
                os.killpg(self.pgid, sig)
            except ProcessLookupError:
                pass
            except OSError as error:
                error_code("SIGNAL_" + str(int(sig)), error)

        def reaped():
            return self.process is None or self.process.poll() is not None

        leader_reaped = reaped()
        group_gone = gone()
        if not group_gone:
            send(signal.SIGTERM)
        term_end = time.monotonic() + min(0.2, max(0, deadline - time.monotonic()) / 2)
        killed = False
        while not (leader_reaped and group_gone):
            now = time.monotonic()
            if not killed and now >= term_end:
                send(signal.SIGKILL)
                killed = True
            leader_reaped, group_gone = reaped(), gone()
            if (leader_reaped and group_gone) or now >= deadline:
                break
            time.sleep(min(0.01, max(0, deadline - time.monotonic())))
        # Never signal a recycled PGID on a later directory-cleanup retry.
        if leader_reaped and group_gone:
            self._stopped = True
        if self._stopped and not errors and not self._removed:
            try:
                self._directory.cleanup()
                self._removed = True
            except OSError as error:
                error_code("DIRECTORY_CLEANUP", error)
        result = CleanupResult(leader_reaped, group_gone, self._removed, tuple(errors))
        self.last_cleanup = result
        return result


class WorkerClient:
    """Nonblocking AF_UNIX single-flight stream; not thread-safe.

    poll returns None on a short deadline (not cancellation). call/wait_ready
    expire fail-closed and disconnect. Native must still close its owner.
    events is a selectors mask; include EVENT_WRITE until pending bytes drain.
    Budgets include the handshake and both directions, including frame headers;
    outgoing bytes are conservatively charged at submit, even before transmission.
    """

    def __init__(self, socketpath, *, max_frame_bytes=MAX_FRAME_BYTES,
                 max_total_bytes=MAX_TOTAL_BYTES):
        if type(max_frame_bytes) is not int or not 4 <= max_frame_bytes <= MAX_FRAME_BYTES:
            raise ValueError("INVALID_FRAME_LIMIT")
        if type(max_total_bytes) is not int or not 4 <= max_total_bytes <= MAX_TOTAL_BYTES:
            raise ValueError("INVALID_TOTAL_LIMIT")
        self.max_frame_bytes = max_frame_bytes
        self.max_total_bytes = max_total_bytes
        self._decoder = IncrementalFrameDecoder(MAX_FRAME_BYTES, MAX_DEPTH, MAX_NODES,
                                                max_total_bytes)
        self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._socket.setblocking(False)
        self._closed = False
        self._connecting = True
        self._outgoing = b""
        self._offset = 0
        self._charged_tx = 0
        self._header = bytearray()
        self._remaining = None
        self._next_seq = 1
        self.pending_seq = None
        self.ready = False
        self.tools = None
        self.last_reply = None
        self.effects_unknown = False
        try:
            code = self._socket.connect_ex(os.fspath(socketpath))
            if code == 0:
                self._connecting = False
            elif code not in (errno.EINPROGRESS, errno.EALREADY, errno.EWOULDBLOCK):
                self._fail("WORKER_CONNECT_FAILED")
        except OSError:
            self._fail("WORKER_CONNECT_FAILED")

    @property
    def total_bytes(self):
        return self._charged_tx + self._decoder.total_bytes

    @property
    def events(self):
        return selectors.EVENT_READ | (selectors.EVENT_WRITE if
            self._connecting or self._outgoing else 0)

    def fileno(self):
        return self._socket.fileno()

    def close(self):
        self._closed = True
        self._socket.close()
        self._outgoing = b""

    def _fail(self, code):
        self.close()
        raise WorkerError(code)

    def _check_open(self):
        if self._closed:
            raise WorkerError("CLIENT_CLOSED")

    def wait_ready(self, deadline):
        _deadline(deadline)
        self._check_open()
        while not self.ready:
            self.poll(deadline)
            if not self.ready and time.monotonic() >= deadline:
                self._fail("HANDSHAKE_TIMEOUT")
        return self.tools

    def submit(self, name, arguments):
        """Queue one bounded request without blocking; returns its sequence."""
        self._check_open()
        if not self.ready:
            raise WorkerError("HANDSHAKE_REQUIRED")
        if self.pending_seq is not None:
            raise WorkerError("REQUEST_IN_FLIGHT")
        if type(name) is not str or not name or type(arguments) is not dict:
            raise ValueError("INVALID_REQUEST")
        seq = self._next_seq
        try:
            data = encode_frame({"type": "child_request", "seq": seq,
                                 "payload": {"name": name, "arguments": arguments}},
                                max_bytes=self.max_frame_bytes,
                                max_depth=MAX_DEPTH, max_nodes=MAX_NODES)
        except ProtocolError as error:
            self._fail(error.code)
        if self.total_bytes + len(data) > self.max_total_bytes:
            self._fail("TOTAL_BYTE_LIMIT")
        self._charged_tx += len(data)
        self._decoder.max_total_bytes = self.max_total_bytes - self._charged_tx
        self._outgoing, self._offset = data, 0
        self.pending_seq = seq
        self.effects_unknown = True
        self.last_reply = None
        self._next_seq += 1
        return seq

    def _frame(self, frame):
        kind, seq, payload = frame["type"], frame["seq"], frame["payload"]
        if kind == "terminate":
            expected = self.pending_seq if self.ready else 0
            if seq != expected or type(payload) is not dict or set(payload) != {"code"} or (
                type(payload["code"]) is not str or
                re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", payload["code"]) is None
            ):
                self._fail("INVALID_TERMINATE")
            self._fail("WORKER_TERMINATED_" + payload["code"])
        if not self.ready:
            if kind != "initial" or seq != 0 or type(payload) is not dict or (
                set(payload) != {"protocol", "tools"} or payload["protocol"] != PROTOCOL or
                type(payload["tools"]) is not list or
                any(type(tool) is not dict for tool in payload["tools"])
            ):
                self._fail("INVALID_HANDSHAKE")
            self.tools = payload["tools"]
            self.ready = True
            self._decoder.max_bytes = self.max_frame_bytes
            return None
        if kind != "child_response" or self.pending_seq is None or seq != self.pending_seq:
            self._fail("RESPONSE_ORDER")
        if self._outgoing or type(payload) is not dict or set(payload) != {"dispatched", "output"} or (
            type(payload["dispatched"]) is not bool
        ):
            self._fail("INVALID_RESPONSE")
        reply = WorkerReply(seq, payload["dispatched"], payload["output"])
        self.pending_seq = None
        self.effects_unknown = False
        self.last_reply = reply
        return reply

    def _receive(self):
        # Read exactly one frame boundary so the post-handshake smaller limit and
        # request pairing take effect before accepting even the next frame header.
        count = 4 - len(self._header) if self._remaining is None else min(65536, self._remaining)
        data = self._socket.recv(count)
        if not data:
            self._decoder.finish()
            self._fail("WORKER_DISCONNECTED")
        if self._remaining is None:
            self._header.extend(data)
            frames = self._decoder.feed(data)
            if len(self._header) == 4:
                self._remaining = struct.unpack(">I", self._header)[0]
        else:
            self._remaining -= len(data)
            frames = self._decoder.feed(data)
        if frames:
            self._header.clear()
            self._remaining = None
            return self._frame(frames[0])
        return None

    def poll(self, deadline):
        """Drive socket progress until reply or absolute deadline; None means pending.

        Passing an expired deadline performs one nonblocking selector turn. JSON
        work is bounded by frame/node limits, not preempted mid-frame by a timer.
        """
        _deadline(deadline)
        self._check_open()
        with selectors.DefaultSelector() as selector:
            selector.register(self._socket, self.events)
            while True:
                selector.modify(self._socket, self.events)
                events = selector.select(max(0, deadline - time.monotonic()))
                if not events:
                    return None
                mask = events[0][1]
                try:
                    if self._connecting:
                        if self._socket.getsockopt(socket.SOL_SOCKET, socket.SO_ERROR):
                            self._fail("WORKER_CONNECT_FAILED")
                        self._connecting = False
                    if mask & selectors.EVENT_WRITE and self._outgoing:
                        sent = self._socket.send(memoryview(self._outgoing)[self._offset:])
                        if sent == 0:
                            self._fail("WORKER_DISCONNECTED")
                        self._offset += sent
                        if self._offset == len(self._outgoing):
                            self._outgoing = b""
                    if mask & selectors.EVENT_READ:
                        was_ready = self.ready
                        reply = self._receive()
                        if reply is not None or (self.ready and not was_ready):
                            return reply
                except BlockingIOError:
                    pass
                except ProtocolError as error:
                    self._fail(error.code)
                except OSError:
                    self._fail("WORKER_IO_FAILED")
                if time.monotonic() >= deadline:
                    return None

    def call(self, name, arguments, *, deadline):
        """Return the original JSON output, without modifying business envelopes."""
        _deadline(deadline)
        self.wait_ready(deadline)
        if time.monotonic() >= deadline:
            self._fail("WORKER_CALL_TIMEOUT")
        self.submit(name, arguments)
        while True:
            reply = self.poll(deadline)
            if time.monotonic() >= deadline:
                if reply is not None:
                    self.pending_seq = reply.seq
                    self.last_reply = None
                    self.effects_unknown = True
                self._fail("WORKER_CALL_TIMEOUT")
            if reply is not None:
                return reply.output
