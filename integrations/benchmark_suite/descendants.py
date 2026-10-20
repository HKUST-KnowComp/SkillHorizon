"""Linux subreaper containment for a serial, otherwise child-free controller."""

import ctypes
import os
from pathlib import Path
import signal
import time


def process_identity(pid):
    try:
        value = Path(f"/proc/{pid}/stat").read_text()
    except (FileNotFoundError, ProcessLookupError):
        return None
    fields = value[value.rfind(")") + 2:].split()
    return {"pid": pid, "ppid": int(fields[1]), "start": int(fields[19])}


def descendants():
    table = {}
    for path in Path("/proc").iterdir():
        if path.name.isdigit():
            row = process_identity(int(path.name))
            if row:
                table[row["pid"]] = row
    owned = {os.getpid()}
    while True:
        expanded = owned | {pid for pid, row in table.items() if row["ppid"] in owned}
        if expanded == owned:
            break
        owned = expanded
    return [table[pid] for pid in owned - {os.getpid()}]


def descendant_rss():
    total = 0
    for row in descendants():
        try:
            resident = int(Path(f"/proc/{row['pid']}/statm").read_text().split()[1])
            current = process_identity(row["pid"])
            if current and current["start"] == row["start"]:
                total += resident * os.sysconf("SC_PAGE_SIZE")
        except (FileNotFoundError, ProcessLookupError):
            continue
    return total


class Containment:
    """Adopt orphaned grandchildren even when the MCP client calls setsid()."""

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        old = ctypes.c_int()
        if self.libc.prctl(37, ctypes.byref(old), 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot read subreaper state")
        self.old = old.value
        if descendants():
            raise ValueError("Candidate controller must have no existing descendants")
        if self.libc.prctl(36, 1, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot establish subreaper containment")
        if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
            self.restore()
            raise RuntimeError("PID-stable Linux signaling is required")

    def restore(self):
        if self.libc.prctl(36, self.old, 0, 0, 0) != 0:
            raise OSError(ctypes.get_errno(), "Cannot restore subreaper state")

    def close(self, process):
        for sig, allowance in ((signal.SIGTERM, .25), (signal.SIGKILL, 5)):
            end = time.monotonic() + allowance
            while True:
                process.poll()
                rows = descendants()
                for row in rows:
                    try:
                        fd = os.pidfd_open(row["pid"])
                    except ProcessLookupError:
                        continue
                    try:
                        current = process_identity(row["pid"])
                        if current and current["start"] == row["start"]:
                            signal.pidfd_send_signal(fd, sig)
                    except ProcessLookupError:
                        pass
                    finally:
                        os.close(fd)
                # Popen owns its leader's exit status; reap only adopted children.
                process.poll()
                for row in rows:
                    if row["pid"] != process.pid:
                        try:
                            os.waitpid(row["pid"], os.WNOHANG)
                        except ChildProcessError:
                            pass
                if not descendants():
                    self.restore()
                    return process.poll() is not None
                if time.monotonic() >= end:
                    break
                time.sleep(.02)
        # Keep orphan adoption active when cleanup is unknown; the caller must stop.
        return False
