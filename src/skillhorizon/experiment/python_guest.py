"""One-shot Linux PID1 supervisor, not a host Python execution backend.

Entrypoint: python -I /harness/python_guest.py. Mount only this file and its
stdlib workspace_state.py sibling read-only. The backend supplies isolated PID
and network namespaces, resource limits, a read-only image, bounded /workspace
and /tmp tmpfs mounts, no-new-privileges, and the five INITIAL_CAPS below.
It must close stdin after one JSON frame and independently verify removal before
exposing a result. A valid guest frame alone does not authorize continuation.

The code child has zero effective/permitted/inheritable/ambient capabilities.
The container bounding set remains inherited: dropping it would require the
unauthorized SETPCAP capability. Non-root saved IDs and no-new-privileges prevent
regaining those capabilities; CPU8 qualification must verify this combination.

Only the independent, unprivileged interpreter receives model code. Importing
this module has no process, privilege, signal, or filesystem side effects.
"""

from contextlib import contextmanager
import ctypes
import json
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import sys
import time


PROTOCOL = "python_execution_v1"
ROOT = Path("/workspace/root")
CODE_UID = CODE_GID = 10001
# The bootstrap read bound is a trusted constant, never an input-frame field.
MAX_PROTOCOL_BYTES = 64 * 1024 * 1024
MAX_CODE_ARG_BYTES = 131071
STARTUP_SECONDS = 20
QUOTAS = {"max_file_bytes", "max_total_bytes", "max_entries", "max_depth", "max_response_chars"}
LIMIT_CEILINGS = {"startup_seconds": 20, "timeout_seconds": 20, "cleanup_seconds": 3, "capture_seconds": 5,
                  "max_stream_bytes": 65536, "max_code_bytes": MAX_CODE_ARG_BYTES,
                  "max_protocol_bytes": MAX_PROTOCOL_BYTES}
INITIAL_CAPS = (1 << 0) | (1 << 2) | (1 << 5) | (1 << 6) | (1 << 7)
SUPERVISOR_CAPS = (1 << 2) | (1 << 5)  # DAC_READ_SEARCH and KILL, never DAC_OVERRIDE.


class GuestError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _assert_boot():
    # This check must precede every entry into OS-mutating guest operations.
    if sys.platform != "linux" or os.getpid() != 1 or os.getuid() != 0 or os.geteuid() != 0:
        raise GuestError("BOOT_REFUSED")


def _load_workspace_state():
    # -I excludes the script directory. This parent is a trusted read-only mount,
    # never cwd, /workspace, PYTHONPATH, or an input-supplied search path.
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import workspace_state
    return workspace_state


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise GuestError("INVALID_INPUT")
        result[key] = value
    return result


def _reject_constant(value):
    raise GuestError("INVALID_INPUT")


def _exact_keys(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise GuestError("INVALID_INPUT")


def validate_request(request, frame_bytes, state):
    _exact_keys(request, {"protocol", "code", "workspace_config", "tree", "limits", "startup_deadline"})
    if request["protocol"] != PROTOCOL or type(request["code"]) is not str:
        raise GuestError("INVALID_INPUT")
    if (type(request["startup_deadline"]) not in (int, float)
            or not math.isfinite(request["startup_deadline"]) or request["startup_deadline"] <= 0):
        raise GuestError("INVALID_INPUT")
    config, limits = request["workspace_config"], request["limits"]
    _exact_keys(config, QUOTAS)
    if any(type(v) is not int or not 0 < v <= MAX_PROTOCOL_BYTES for v in config.values()):
        raise GuestError("INVALID_INPUT")
    _exact_keys(limits, LIMIT_CEILINGS)
    for key, ceiling in LIMIT_CEILINGS.items():
        value = limits[key]
        types = (int, float) if key.endswith("_seconds") else (int,)
        if type(value) not in types or not 0 < value <= ceiling:
            raise GuestError("INVALID_INPUT")
    # Reserve enough for an unknown-state envelope including both fully escaped
    # streams. A post-tree that will not fit is rejected as a whole, never cut.
    if (limits["max_protocol_bytes"] < 2048 + 12 * limits["max_stream_bytes"]
            or frame_bytes > limits["max_protocol_bytes"]):
        raise GuestError("PROTOCOL_LIMIT")
    try:
        code_bytes = request["code"].encode("utf-8")
    except UnicodeError:
        raise GuestError("INVALID_INPUT") from None
    # Linux's conservative per-argv ceiling includes the terminating NUL.
    if (b"\0" in code_bytes or len(code_bytes) > limits["max_code_bytes"]
            or len(code_bytes) > MAX_CODE_ARG_BYTES):
        raise GuestError("CODE_LIMIT")
    if type(request["tree"]) is not dict:
        raise GuestError("INVALID_INPUT")
    # This shared validator owns base64 decoding, exact lengths, roots, path
    # topology and all five B quotas; there is no second guest tree dialect.
    state.validate_snapshot(request["tree"], config)
    return request


def read_request(stream, state):
    data = bytearray()
    while True:
        chunk = stream.read(min(65536, MAX_PROTOCOL_BYTES + 1 - len(data)))
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_PROTOCOL_BYTES:
            raise GuestError("PROTOCOL_LIMIT")
    try:
        request = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                             parse_constant=_reject_constant)
        return validate_request(request, len(data), state)
    except (ValueError, UnicodeError, RecursionError, OverflowError):
        raise GuestError("INVALID_INPUT") from None


def _new_result():
    return {"exit_code": None, "timed_out": False, "stdout": "", "stderr": "",
            "stdout_truncated": False, "stderr_truncated": False,
            "wall_seconds": 0.0, "writers_cleared": False}


def _envelope(result, tree=None, error_code=None):
    return {"protocol": PROTOCOL, "valid": error_code is None, "tree": tree,
            "result": result, "error_code": error_code}


def encode_frame(envelope, limit):
    output = bytearray()
    encoder = json.JSONEncoder(ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    for piece in encoder.iterencode(envelope):
        encoded = piece.encode("ascii")
        if len(output) + len(encoded) + 1 > limit:
            raise GuestError("PROTOCOL_LIMIT")
        output.extend(encoded)
    output.extend(b"\n")
    return bytes(output)


@contextmanager
def _deadline(seconds, error_code):
    _assert_boot()

    def expired(signum, frame):
        raise GuestError(error_code)

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


class _CapHeader(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint32), ("pid", ctypes.c_int)]


class _CapData(ctypes.Structure):
    _fields_ = [("effective", ctypes.c_uint32), ("permitted", ctypes.c_uint32),
                ("inheritable", ctypes.c_uint32)]


class _Privileges:
    def __init__(self):
        _assert_boot()
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.prctl.restype = ctypes.c_int
        self.libc.prctl.argtypes = [ctypes.c_int] + [ctypes.c_ulong] * 4
        for name in ("capget", "capset"):
            function = getattr(self.libc, name)
            function.restype = ctypes.c_int
            function.argtypes = [ctypes.POINTER(_CapHeader), ctypes.POINTER(_CapData)]

    def prctl(self, option, arg=0, arg3=0):
        value = self.libc.prctl(option, arg, arg3, 0, 0)
        if value == -1:
            raise GuestError("PRIVILEGE_FAILURE")
        return value

    def capabilities(self, mask=None):
        header = _CapHeader(0x20080522, 0)
        data = (_CapData * 2)()
        if mask is not None:
            for index in range(2):
                data[index].effective = data[index].permitted = (mask >> (32 * index)) & 0xffffffff
            if self.libc.capset(ctypes.byref(header), data) != 0:
                raise GuestError("PRIVILEGE_FAILURE")
        if self.libc.capget(ctypes.byref(header), data) != 0:
            raise GuestError("PRIVILEGE_FAILURE")
        return tuple(sum(getattr(data[i], field) << (32 * i) for i in range(2))
                     for field in ("effective", "permitted", "inheritable"))

    def initialize(self):
        _assert_boot()
        if self.capabilities() != (INITIAL_CAPS, INITIAL_CAPS, 0):
            raise GuestError("PRIVILEGE_FAILURE")
        # Require the backend's no-new-privileges policy, not just our own set.
        if self.prctl(39) != 1:  # PR_GET_NO_NEW_PRIVS
            raise GuestError("PRIVILEGE_FAILURE")
        self.prctl(47, 4)  # PR_CAP_AMBIENT / CLEAR_ALL
        self.prctl(4, 0)  # PR_SET_DUMPABLE: protects /proc/1/fd and process memory.
        if self.prctl(3) != 0:
            raise GuestError("PRIVILEGE_FAILURE")
        signal.signal(signal.SIGCHLD, signal.SIG_DFL)
        os.set_inheritable(0, False)
        os.set_inheritable(1, False)
        os.set_inheritable(2, False)

    def child_setup(self):
        # Trusted fork setup only: never eval/exec model code in this interpreter.
        # Popen calls this after installing the private stdin/stdout/stderr.
        if (sys.platform != "linux" or os.getpid() == 1 or os.getppid() != 1
                or os.getuid() != 0):
            raise GuestError("BOOT_REFUSED")
        self.prctl(38, 1)  # PR_SET_NO_NEW_PRIVS
        self.prctl(8, 0)  # PR_SET_KEEPCAPS
        self.prctl(47, 4)
        os.setgroups([])
        os.setresgid(CODE_GID, CODE_GID, CODE_GID)
        os.setresuid(CODE_UID, CODE_UID, CODE_UID)
        if self.capabilities(0) != (0, 0, 0):
            raise GuestError("PRIVILEGE_FAILURE")
        if (os.getresuid() != (CODE_UID,) * 3 or os.getresgid() != (CODE_GID,) * 3
                or os.getgroups() or self.prctl(39) != 1):
            raise GuestError("PRIVILEGE_FAILURE")
        self.prctl(4, 0)
        os.umask(0o077)

    def shrink(self):
        _assert_boot()
        if self.capabilities(SUPERVISOR_CAPS) != (SUPERVISOR_CAPS, SUPERVISOR_CAPS, 0):
            raise GuestError("PRIVILEGE_FAILURE")
        self.prctl(47, 4)
        self.prctl(4, 0)
        if self.prctl(3) != 0 or self.prctl(39) != 1:
            raise GuestError("PRIVILEGE_FAILURE")


def _owned_directory(path):
    info = path.lstat()
    if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0
            or info.st_gid != 0 or info.st_mode & 0o022):
        raise GuestError("UNSAFE_ROOT")


def _prepare_workspace(request, state):
    _assert_boot()
    for path in (Path("/"), ROOT.parent):
        _owned_directory(path)
    with open("/proc/self/mountinfo", encoding="ascii") as handle:
        mounts = [line.split() for line in handle]
    if not any(row[4] == "/workspace" and row[row.index("-") + 1] == "tmpfs"
               for row in mounts):
        raise GuestError("UNSAFE_ROOT")
    if os.path.lexists(ROOT):
        raise GuestError("ROOT_NOT_FRESH")
    tree, config = request["tree"], request["workspace_config"]
    state.restore_snapshot(ROOT, tree, config)
    _owned_directory(ROOT)
    if state.workspace_snapshot(ROOT, config) != tree:
        raise GuestError("RESTORE_MISMATCH")
    os.chmod(ROOT, 0o755)
    for path, row in sorted(tree.items()):
        target = ROOT / path
        readonly = path == "inputs" or path.startswith("inputs/")
        owner = 0 if readonly else CODE_UID
        mode = (0o555 if readonly else 0o700) if row["type"] == "directory" else (0o444 if readonly else 0o600)
        # No CAP_FOWNER: normalize mode while still root-owned, then transfer.
        os.chmod(target, mode)
        os.chown(target, owner, 0 if readonly else CODE_GID, follow_symlinks=False)


class _StreamCapture:
    """Retain at most limit raw bytes per stream, decode UTF-8 with replacement."""

    def __init__(self, limit):
        self.limit = limit
        self.buffers = {"stdout": bytearray(), "stderr": bytearray()}
        self.truncated = {"stdout": False, "stderr": False}
        self.selector = selectors.DefaultSelector()

    def attach(self, process):
        for name in self.buffers:
            handle = getattr(process, name)
            os.set_blocking(handle.fileno(), False)
            self.selector.register(handle, selectors.EVENT_READ, name)

    def feed(self, name, chunk):
        available = self.limit - len(self.buffers[name])
        self.buffers[name].extend(chunk[:available])
        self.truncated[name] |= len(chunk) > available

    def drain(self, timeout):
        # One bounded read per ready descriptor keeps either stream, deadlines
        # and waitpid responsive even under a continuous stdout/stderr flood.
        for key, events in self.selector.select(timeout):
            try:
                chunk = os.read(key.fd, 65536)
            except BlockingIOError:
                continue
            if chunk:
                self.feed(key.data, chunk)
            else:
                self.selector.unregister(key.fileobj)

    def pending(self):
        return bool(self.selector.get_map())

    def apply(self, result):
        for name, data in self.buffers.items():
            result[name] = bytes(data).decode("utf-8", errors="replace")
            result[name + "_truncated"] = self.truncated[name]

    def close(self):
        self.selector.close()


def _namespace_pids():
    _assert_boot()
    # Reject a host /proc bind mounted into an otherwise isolated PID namespace.
    if os.readlink("/proc/self") != "1":
        raise GuestError("UNSAFE_PROC")
    with os.scandir("/proc") as entries:
        return [int(entry.name) for entry in entries if entry.name.isascii()
                and entry.name.isdecimal() and int(entry.name) > 1]


def _reap(process, deadline):
    _assert_boot()
    while time.monotonic() < deadline:
        try:
            pid, status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return True
        except InterruptedError:
            continue
        if pid == 0:
            return False
        if process is not None and pid == process.pid:
            process.returncode = os.waitstatus_to_exitcode(status)
    return False


def _clear_writers(process, capture, seconds):
    _assert_boot()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        _reap(process, deadline)
        for pid in _namespace_pids():
            if time.monotonic() >= deadline:
                return False
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        no_children = _reap(process, deadline)
        empty = not _namespace_pids()
        if capture is not None:
            capture.drain(0)
        if empty and no_children:
            # No writer remains; consume already-buffered pipe bytes through EOF.
            while capture is not None and capture.pending():
                if time.monotonic() >= deadline:
                    return False
                capture.drain(min(0.01, max(0, deadline - time.monotonic())))
            return time.monotonic() < deadline
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    return False


def _spawn(code, privileges):
    _assert_boot()
    return subprocess.Popen(
        [sys.executable, "-I", "-c", code], cwd=ROOT,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        close_fds=True, pass_fds=(), shell=False, bufsize=0,
        env={"PATH": "/usr/local/bin:/usr/bin:/bin", "HOME": "/nonexistent",
             "TMPDIR": "/tmp", "LANG": "C.UTF-8"},
        preexec_fn=privileges.child_setup,
    )


def _monitor(process, capture, seconds, result):
    deadline = time.monotonic() + seconds
    while True:
        _reap(process, deadline)
        if process.returncode is not None:
            return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            result["timed_out"] = True
            return
        capture.drain(min(0.05, remaining))


def _inputs(tree):
    return {path: row for path, row in tree.items() if path == "inputs" or path.startswith("inputs/")}


def execute_request(request, state, privileges, startup_deadline=None, *,
                    _spawn_hook=None, _monitor_hook=None, _encode_hook=None):
    _assert_boot()
    result = _new_result()
    started = time.monotonic()
    limits = request["limits"]
    capture = _StreamCapture(limits["max_stream_bytes"])
    process = None
    cleanup_needed = False
    cleanup_attempted = False
    tree = None
    error = None
    try:
        now = time.monotonic()
        startup_deadline = min(request["startup_deadline"],
                               now + limits["startup_seconds"] if startup_deadline is None else startup_deadline)
        remaining = startup_deadline - now
        if remaining <= 0:
            raise GuestError("STARTUP_TIMEOUT")
        with _deadline(remaining, "STARTUP_TIMEOUT"):
            if _namespace_pids():
                raise GuestError("UNSAFE_PROC")
            _prepare_workspace(request, state)
            cleanup_needed = True  # Popen itself can fail after fork.
            try:
                process = (_spawn if _spawn_hook is None else _spawn_hook)(request["code"], privileges)
            finally:
                privileges.shrink()
            capture.attach(process)
        (_monitor if _monitor_hook is None else _monitor_hook)(
            process, capture, limits["timeout_seconds"], result)
        cleanup_attempted = True
        result["writers_cleared"] = _clear_writers(process, capture, limits["cleanup_seconds"])
        if not result["writers_cleared"]:
            raise GuestError("CLEANUP_TIMEOUT")
        with _deadline(limits["capture_seconds"], "CAPTURE_TIMEOUT"):
            tree = state.workspace_snapshot(ROOT, request["workspace_config"])
            state.validate_snapshot(tree, request["workspace_config"])
            if _inputs(tree) != _inputs(request["tree"]):
                raise GuestError("INPUTS_CHANGED")
            capture.apply(result)
            if process.returncode is None:
                raise GuestError("EXIT_STATUS_UNKNOWN")
            result["exit_code"] = process.returncode
            result["wall_seconds"] = max(0.0, time.monotonic() - started)
            # Encoding is also part of the capture deadline and frame budget.
            return (encode_frame if _encode_hook is None else _encode_hook)(
                _envelope(result, tree), limits["max_protocol_bytes"])
    except GuestError as exc:
        error = exc.code
    except (state.WorkspaceError, state.WorkspaceFatalError):
        error = "STATE_UNKNOWN"
    except Exception:
        # No exception text, model code, private paths, or partial tree leaks.
        error = "SUPERVISOR_ERROR"
    finally:
        if cleanup_needed and not cleanup_attempted:
            try:
                result["writers_cleared"] = _clear_writers(process, capture, limits["cleanup_seconds"])
            except Exception:
                result["writers_cleared"] = False
            if not result["writers_cleared"]:
                error = "CLEANUP_TIMEOUT"
        capture.apply(result)
        capture.close()
        if process is not None:
            result["exit_code"] = process.returncode
            for handle in (process.stdout, process.stderr):
                if handle is not None:
                    handle.close()
        result["wall_seconds"] = max(0.0, time.monotonic() - started)
    return (encode_frame if _encode_hook is None else _encode_hook)(
        _envelope(result, error_code=error), limits["max_protocol_bytes"])


def main():
    limit = MAX_PROTOCOL_BYTES
    try:
        _assert_boot()  # Refuse before reading input or importing the helper.
        startup_started = time.monotonic()
        with _deadline(STARTUP_SECONDS, "STARTUP_TIMEOUT"):
            privileges = _Privileges()
            privileges.initialize()
            state = _load_workspace_state()
            request = read_request(sys.stdin.buffer, state)
        limit = request["limits"]["max_protocol_bytes"]
        # The fixed local Docker endpoint shares CLOCK_MONOTONIC with its host.
        # Time spent starting/attaching to the container cannot restart the clock.
        startup_deadline = min(startup_started + request["limits"]["startup_seconds"],
                               request["startup_deadline"])
        frame = execute_request(request, state, privileges, startup_deadline)
    except GuestError as exc:
        frame = encode_frame(_envelope(_new_result(), error_code=exc.code), limit)
    except Exception:
        frame = encode_frame(_envelope(_new_result(), error_code="INVALID_INPUT"), limit)
    try:
        sys.stdout.buffer.write(frame)
        sys.stdout.buffer.flush()
    except (BrokenPipeError, OSError):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
