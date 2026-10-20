"""Stage E Docker-only guest, reusing the complete D security lifecycle.

Entrypoint: python -I /harness/ptc_guest.py. Initial/final are length frames;
stdin remains open. Child code inherits only its private socketpair endpoint.
The host still owns authorization, durable admission and business quiescence.
Importing this module in a package does not start processes or mutate the OS.
"""

import json
import os
from pathlib import Path
import selectors
import socket
import struct
import sys
import tempfile
import time

if __package__:
    from . import python_guest as guest
    from . import ptc_protocol as protocol
else:
    # Isolated Python excludes the script directory. Do not load helpers on a
    # host invocation, and never search a model-controlled directory.
    if (sys.platform != "linux" or os.getpid() != 1
            or os.getuid() != 0 or os.geteuid() != 0):
        raise SystemExit("PTC_BOOT_REFUSED")
    sys.path.insert(0, "/harness")
    import python_guest as guest
    import ptc_protocol as protocol


TERMINAL_RESERVE_BYTES = 16384
MAX_FRAME_BYTES = guest.MAX_PROTOCOL_BYTES + TERMINAL_RESERVE_BYTES
OUTER_MAX_DEPTH = 40
OUTER_MAX_NODES = 1000000
OUTER_SECONDS = 60
# Resource-only validation is deliberately stdlib-only: ptc_config imports
# host execution modules that must not be mounted into the guest.
_RANGES = {
    "max_children_per_program": (1, 64), "max_children_per_episode": (1, 4096),
    "max_request_bytes": (1024, 1024**2), "max_response_bytes": (1024, 4 * 1024**2),
    "max_bridge_bytes": (2048, 64 * 1024**2),
    "max_json_depth": (4, 32), "max_json_nodes": (64, 65536),
}


def validate_config(config):
    if type(config) is not dict or set(config) != set(_RANGES):
        raise guest.GuestError("PTC_CONFIG_INVALID")
    for key, (low, high) in _RANGES.items():
        if type(config[key]) is not int or not low <= config[key] <= high:
            raise guest.GuestError("PTC_CONFIG_INVALID")
    if (config["max_children_per_episode"] < config["max_children_per_program"]
            or config["max_bridge_bytes"] < config["max_request_bytes"] + config["max_response_bytes"]):
        raise guest.GuestError("PTC_CONFIG_INVALID")
    return dict(config)


def _read_exact(stream, size):
    output = bytearray()
    while len(output) < size:
        part = stream.read(min(65536, size - len(output)))
        if not part:
            raise guest.GuestError("PTC_TRUNCATED_INITIAL")
        output.extend(part)
    return bytes(output)


def read_initial(stream, state):
    header = _read_exact(stream, 4)
    length = struct.unpack(">I", header)[0]
    if not 0 < length <= MAX_FRAME_BYTES - 4:
        raise guest.GuestError("PTC_INITIAL_LIMIT")
    decoder = protocol.IncrementalFrameDecoder(
        MAX_FRAME_BYTES, OUTER_MAX_DEPTH, OUTER_MAX_NODES, MAX_FRAME_BYTES)
    frames = decoder.feed(header + _read_exact(stream, length))
    decoder.finish()
    frame = frames[0]
    if (frame["type"] != "initial" or frame["seq"] != 0
            or type(frame["payload"]) is not dict
            or set(frame["payload"]) != {"execution", "ptc"}):
        raise guest.GuestError("PTC_INITIAL_INVALID")
    execution = frame["payload"]["execution"]
    # Reuse D's exact request/tree/limit dialect, including its argv ceiling.
    guest.validate_request(execution, 0, state)
    limit = execution["limits"]["max_protocol_bytes"]
    guest.encode_frame(execution, limit)
    if length + 4 > limit + TERMINAL_RESERVE_BYTES:
        raise guest.GuestError("PTC_INITIAL_LIMIT")
    return execution, validate_config(frame["payload"]["ptc"])


class _SingleFrame:
    """A bounded partial frame, rejecting even a partial second frame in a read."""

    def __init__(self, limit, depth, nodes):
        self.limit, self.depth, self.nodes = limit, depth, nodes
        self.data = bytearray()
        self.size = None

    def feed(self, chunk):
        if len(self.data) + len(chunk) > self.limit:
            raise guest.GuestError("PTC_FRAME_LIMIT")
        self.data.extend(chunk)
        if self.size is None and len(self.data) >= 4:
            self.size = 4 + struct.unpack(">I", self.data[:4])[0]
            if not 4 < self.size <= self.limit:
                raise guest.GuestError("PTC_FRAME_LIMIT")
        if self.size is None or len(self.data) < self.size:
            return None
        if len(self.data) != self.size:
            raise guest.GuestError("PTC_FRAME_ORDER")
        raw = bytes(self.data)
        decoder = protocol.IncrementalFrameDecoder(self.limit, self.depth, self.nodes, self.limit)
        frames = decoder.feed(raw)
        decoder.finish()
        self.data.clear()
        self.size = None
        return frames[0], raw


class _Forwarder:
    """Pure direction/sequence and actual-wire accounting; grants no authority."""

    def __init__(self, config):
        self.config = validate_config(config)
        depth, nodes = config["max_json_depth"], config["max_json_nodes"]
        self.child = _SingleFrame(config["max_request_bytes"], depth, nodes)
        self.host = _SingleFrame(max(config["max_response_bytes"], TERMINAL_RESERVE_BYTES), depth, nodes)
        self.seq = 0
        self.inflight = None
        self.bridge_bytes = 0
        self.closed = False
        self.host_pending = b""
        self.child_pending = b""

    def from_child(self, chunk):
        if self.closed or self.inflight is not None or self.child_pending:
            raise guest.GuestError("PTC_CHILD_ORDER")
        # Count partial and noncanonical input too; never count only re-encoding.
        if self.bridge_bytes + len(chunk) > self.config["max_bridge_bytes"]:
            raise guest.GuestError("PTC_BRIDGE_LIMIT")
        self.bridge_bytes += len(chunk)
        parsed = self.child.feed(chunk)
        if parsed is None:
            return
        frame, raw = parsed
        payload = frame["payload"]
        if (frame["type"] != "child_request" or frame["seq"] != self.seq + 1
                or type(payload) is not dict or set(payload) != {"name", "arguments"}
                or type(payload["name"]) is not str or type(payload["arguments"]) is not dict):
            raise guest.GuestError("PTC_CHILD_REQUEST_INVALID")
        self.seq = self.inflight = frame["seq"]
        self.host_pending = raw

    def from_host(self, chunk):
        if self.closed or self.inflight is None or self.host_pending or self.child_pending:
            raise guest.GuestError("PTC_HOST_ORDER")
        parsed = self.host.feed(chunk)
        if parsed is None:
            return
        frame, raw = parsed
        if frame["seq"] != self.inflight or frame["type"] not in ("child_response", "terminate"):
            raise guest.GuestError("PTC_HOST_ORDER")
        if type(frame["payload"]) is not dict:
            raise guest.GuestError("PTC_RESPONSE_INVALID")
        if frame["type"] == "terminate":
            payload = frame["payload"]
            code = payload.get("code")
            if (set(payload) != {"code"} or type(code) is not str or not code
                    or len(code) > 128 or any(c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for c in code)
                    or len(raw) > TERMINAL_RESERVE_BYTES):
                raise guest.GuestError("PTC_TERMINAL_INVALID")
            # Terminal metadata is a separate fixed allowance, not business
            # traffic. The authoritative host has confirmed no unknown effect.
            self.closed = True
            self.inflight = None
        else:
            if len(raw) > self.config["max_response_bytes"]:
                raise guest.GuestError("PTC_RESPONSE_LIMIT")
            if self.config["max_response_bytes"] < self.host.limit:
                protocol.encode_frame(frame, max_bytes=self.config["max_response_bytes"],
                                      max_depth=self.config["max_json_depth"],
                                      max_nodes=self.config["max_json_nodes"])
            if self.bridge_bytes + len(raw) > self.config["max_bridge_bytes"]:
                raise guest.GuestError("PTC_BRIDGE_LIMIT")
            self.bridge_bytes += len(raw)
        self.child_pending = raw

    def delivered(self):
        if self.child_pending:
            raise guest.GuestError("PTC_RESPONSE_UNDELIVERED")
        self.inflight = None

    def child_exited(self):
        if self.inflight is not None or self.host_pending or self.child.data or self.host.data:
            raise guest.GuestError("PTC_CHILD_EXIT_INFLIGHT")


def _write_some(fd, data):
    try:
        count = os.write(fd, memoryview(data)[:65536])
    except BlockingIOError:
        return data
    if count <= 0:
        raise guest.GuestError("PTC_WRITE_FAILED")
    return data[count:]


class _Hooks:
    def __init__(self, config, seconds, input_fd, output_fd):
        self.forwarder = _Forwarder(config)
        self.seconds = seconds
        self.input_fd, self.output_fd = input_fd, output_fd
        self.channel = None
        self.code_dir = None
        self.deadline = None

    def spawn(self, code, privileges):
        guest._assert_boot()
        self.code_dir = Path(tempfile.mkdtemp(prefix="ptc-", dir="/tmp"))
        code_path = self.code_dir / "code.py"
        with code_path.open("xb") as handle:
            handle.write(code.encode("utf-8"))
            os.fchmod(handle.fileno(), 0o444)
        os.chmod(self.code_dir, 0o755)
        self.channel, child = socket.socketpair(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            fd = child.fileno()
            if fd < 3 or fd in (self.input_fd, self.output_fd):
                raise guest.GuestError("PTC_FD_INVALID")
            self.deadline = time.monotonic() + self.seconds
            return guest.subprocess.Popen(
                [sys.executable, "-I", "/harness/ptc_bootstrap.py", str(fd),
                 json.dumps(self.forwarder.config, separators=(",", ":")),
                 repr(self.deadline), str(code_path)], cwd=guest.ROOT,
                stdin=guest.subprocess.DEVNULL, stdout=guest.subprocess.PIPE, stderr=guest.subprocess.PIPE,
                close_fds=True, pass_fds=(fd,), shell=False, bufsize=0,
                env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
                     "TMPDIR": "/tmp", "LANG": "C.UTF-8"},
                preexec_fn=privileges.child_setup,
            )
        finally:
            child.close()

    def monitor(self, process, capture, seconds, result):
        guest._assert_boot()
        forwarding = self.forwarder
        child_fd = self.channel.fileno()
        for fd in (child_fd, self.input_fd, self.output_fd):
            os.set_blocking(fd, False)
        with selectors.DefaultSelector() as selector:
            selector.register(self.input_fd, selectors.EVENT_READ, "host")
            selector.register(child_fd, selectors.EVENT_READ, "child")
            child_open = True
            while True:
                capture.drain(0)
                guest._reap(process, self.deadline)
                remaining = self.deadline - time.monotonic()
                if remaining <= 0:
                    result["timed_out"] = True
                    # The host reconciles any issued call after the existing
                    # writer cleanup and snapshot, without running more code.
                    return
                # Register output only while queued, avoiding a writable-pipe
                # busy loop. Both buffers are at most one bounded frame.
                if forwarding.host_pending:
                    if self.output_fd not in selector.get_map():
                        selector.register(self.output_fd, selectors.EVENT_WRITE, "output")
                elif self.output_fd in selector.get_map():
                    selector.unregister(self.output_fd)
                if child_open:
                    selector.modify(child_fd, selectors.EVENT_READ |
                                    (selectors.EVENT_WRITE if forwarding.child_pending else 0), "child")
                events = selector.select(0 if process.returncode is not None else min(0.05, remaining))
                # Read child first: pipelining cannot hide behind a simultaneously
                # ready host response/write that would clear the active request.
                events.sort(key=lambda item: item[0].data != "child")
                for key, mask in events:
                    if time.monotonic() >= self.deadline:
                        result["timed_out"] = True
                        return
                    if mask & selectors.EVENT_READ:
                        try:
                            chunk = os.read(key.fd, 65536)
                        except BlockingIOError:
                            continue
                        if key.data == "child":
                            if chunk:
                                forwarding.from_child(chunk)
                            else:
                                if time.monotonic() >= self.deadline:
                                    result["timed_out"] = True
                                    return
                                forwarding.child_exited()
                                selector.unregister(child_fd)
                                child_open = False
                        elif chunk:
                            forwarding.from_host(chunk)
                        else:
                            if not forwarding.closed:
                                raise guest.GuestError("PTC_HOST_DISCONNECTED")
                            selector.unregister(self.input_fd)
                    if mask & selectors.EVENT_WRITE:
                        if time.monotonic() >= self.deadline:
                            result["timed_out"] = True
                            return
                        if key.data == "output":
                            forwarding.host_pending = _write_some(key.fd, forwarding.host_pending)
                        elif child_open:
                            forwarding.child_pending = _write_some(key.fd, forwarding.child_pending)
                            if not forwarding.child_pending:
                                forwarding.delivered()
                                if forwarding.closed:
                                    selector.unregister(child_fd)
                                    child_open = False
                                    self.channel.close()
                if process.returncode is not None:
                    if time.monotonic() >= self.deadline:
                        result["timed_out"] = True
                        return
                    forwarding.child_exited()
                    return

    def close(self):
        if self.channel is not None:
            self.channel.close()
        if self.code_dir is not None:
            (self.code_dir / "code.py").unlink(missing_ok=True)
            self.code_dir.rmdir()


def execute_request(request, config, state, privileges, startup_deadline=None, *, input_fd=0, output_fd=1):
    guest._assert_boot()
    hooks = _Hooks(config, request["limits"]["timeout_seconds"], input_fd, output_fd)
    try:
        return guest.execute_request(request, state, privileges, startup_deadline,
                                     _spawn_hook=hooks.spawn, _monitor_hook=hooks.monitor,
                                     _encode_hook=final_frame)
    finally:
        hooks.close()


def final_frame(envelope, limit):
    if envelope["result"]["writers_cleared"] is not True:
        raise guest.GuestError("PTC_WRITERS_NOT_CLEARED")
    guest.encode_frame(envelope, limit)  # PTC overhead cannot enlarge the D envelope budget.
    try:
        return protocol.encode_frame({"type": "final", "seq": 0, "payload": envelope},
                                     max_bytes=min(MAX_FRAME_BYTES, limit + TERMINAL_RESERVE_BYTES),
                                     max_depth=OUTER_MAX_DEPTH, max_nodes=OUTER_MAX_NODES)
    except protocol.ProtocolError:
        raise guest.GuestError("PROTOCOL_LIMIT") from None


def _write_final(fd, frame, deadline):
    os.set_blocking(fd, False)
    with selectors.DefaultSelector() as selector:
        selector.register(fd, selectors.EVENT_WRITE)
        while frame:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise guest.GuestError("PTC_FINAL_TIMEOUT")
            if selector.select(min(0.05, remaining)):
                frame = _write_some(fd, frame)


def main():
    try:
        guest._assert_boot()
        started = time.monotonic()
        with guest._deadline(guest.STARTUP_SECONDS, "STARTUP_TIMEOUT"):
            privileges = guest._Privileges()
            privileges.initialize()
            state = guest._load_workspace_state()
            # Unbuffered reads leave any bytes after initial visible to selector.
            with os.fdopen(os.dup(0), "rb", buffering=0) as stream:
                request, config = read_initial(stream, state)
        startup = min(started + request["limits"]["startup_seconds"], request["startup_deadline"])
        frame = execute_request(request, config, state, privileges, startup)
        limits = request["limits"]
        # The host already reserved this one-second wire margin. It remains
        # authoritative and may cut transport earlier; no new resource window.
        finish_by = min(started + OUTER_SECONDS, startup + limits["timeout_seconds"]
                        + limits["cleanup_seconds"] + limits["capture_seconds"] + 1)
        _write_final(1, frame, finish_by)
        return 0
    except Exception:
        # No final without confirmed namespace quiescence. The host retains
        # Docker removal and outstanding-business cleanup responsibility.
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
