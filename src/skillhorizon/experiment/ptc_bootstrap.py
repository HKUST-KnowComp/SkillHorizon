"""Trusted child bootstrap; only the Docker PID1 supervisor may launch it.

Model source is a root-owned, read-only file outside the logical workspace.
No source prefix or source-sized argument is added to the D code limit.
"""

import json
import os
from pathlib import Path
import stat
import sys


def _assert_child():
    if (sys.platform != "linux" or os.getpid() == 1 or os.getppid() != 1
            or os.getresuid() != (10001,) * 3 or os.getresgid() != (10001,) * 3
            or os.getgroups()):
        raise RuntimeError("PTC_BOOT_REFUSED")
    with open("/proc/self/status", encoding="ascii") as handle:
        fields = dict(line.rstrip().split(":", 1) for line in handle if ":" in line)
    if (any(int(fields[key].strip(), 16) != 0
            for key in ("CapInh", "CapPrm", "CapEff", "CapAmb"))
            or fields.get("NoNewPrivs", "").strip() != "1"):
        raise RuntimeError("PTC_BOOT_REFUSED")


def main():
    _assert_child()  # Must precede loading helpers or reading/executing source.
    if len(sys.argv) != 5:
        raise RuntimeError("PTC_BOOT_ARGUMENTS")
    fd, config, deadline = int(sys.argv[1]), json.loads(sys.argv[2]), float(sys.argv[3])
    path = Path(sys.argv[4])
    if path.parent.parent != Path("/tmp") or path.name != "code.py":
        raise RuntimeError("PTC_CODE_PATH")
    parent, info = path.parent.lstat(), path.lstat()
    if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid != 0 or parent.st_mode & 0o022
            or not stat.S_ISREG(info.st_mode) or info.st_uid != 0
            or info.st_mode & 0o222 or info.st_size > 131071):
        raise RuntimeError("PTC_CODE_PATH")
    sys.path.insert(0, "/harness")
    import ptc_guest_client
    sys.modules["skillhorizon_tools"] = ptc_guest_client
    ptc_guest_client._initialize(fd, config, deadline)
    source = path.read_bytes()
    sys.argv = ["-c"]
    namespace = {"__name__": "__main__", "__builtins__": __builtins__}
    exec(compile(source, "<ptc>", "exec"), namespace, namespace)


if __name__ == "__main__":
    main()
