"""Bounded per-run resource samples; maxima here are not true container peaks."""

import os
from pathlib import Path
import re
import resource
import subprocess
import threading
import time


_MEMORY = re.compile(r"^([0-9]+(?:\.[0-9]+)?)(B|kB|MB|GB|TB|KiB|MiB|GiB|TiB)$")
_SCALE = {"B": 1, "kB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
          "KiB": 1024, "MiB": 1024 ** 2, "GiB": 1024 ** 3, "TiB": 1024 ** 4}


def memory_bytes(value):
    match = _MEMORY.fullmatch(value.strip())
    if match is None:
        raise ValueError("Unsupported Docker memory unit")
    return round(float(match[1]) * _SCALE[match[2]])


def directory_bytes(root, *, deadline=None, stop_event=None):
    total = 0
    stack = [Path(root)]
    while stack:
        if ((deadline is not None and time.monotonic() >= deadline)
                or (stop_event is not None and stop_event.is_set())):
            raise TimeoutError("Output directory sampling expired")
        directory = stack.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                if ((deadline is not None and time.monotonic() >= deadline)
                        or (stop_event is not None and stop_event.is_set())):
                    raise TimeoutError("Output directory sampling expired")
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False):
                    total += entry.stat(follow_symlinks=False).st_size
    if deadline is not None and time.monotonic() >= deadline:
        raise TimeoutError("Output directory sampling expired")
    return total


class ResourceSamples:
    def __init__(self, output, container_workdir, *, interval=30):
        self.output = Path(output)
        self.container_workdir = Path(container_workdir).resolve()
        self.interval = interval
        self.started = time.monotonic()
        self.samples = []
        self.errors = []
        self.event = threading.Event()
        self.thread = None

    def _docker_memory(self):
        listing = subprocess.run(["docker", "ps", "--filter",
            f"label=com.docker.compose.project.working_dir={self.container_workdir}",
            "--format", "{{.ID}}"], capture_output=True, text=True, timeout=15, check=True)
        ids = listing.stdout.split()
        if not ids:
            return 0, 0
        stats = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", *ids],
            capture_output=True, text=True, timeout=20, check=True)
        usage = [memory_bytes(line.split(" / ", 1)[0]) for line in stats.stdout.splitlines() if line]
        if len(usage) != len(ids):
            raise ValueError("Incomplete attributable Docker stats")
        return len(ids), sum(usage)

    def sample(self):
        value = {"elapsed_seconds": round(time.monotonic() - self.started, 3)}
        try:
            value["output_bytes"] = directory_bytes(self.output, deadline=time.monotonic() + 5)
        except (OSError, ValueError, TimeoutError) as error:
            self.errors.append({"source": "output", "type": type(error).__name__})
        try:
            value["container_count"], value["container_memory_bytes"] = self._docker_memory()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            self.errors.append({"source": "docker", "type": type(error).__name__})
        self.samples.append(value)

    def start(self):
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def _loop(self):
        self.sample()
        while not self.event.wait(self.interval):
            self.sample()
        self.sample()

    def finish(self):
        self.event.set()
        if self.thread is not None:
            try:
                self.thread.join(timeout=10)
            except RuntimeError:
                self.errors.append({"source": "sampler", "type": "thread_not_started"})
            else:
                if self.thread.is_alive():
                    self.errors.append({"source": "sampler", "type": "join_timeout"})
        samples = [sample.copy() for sample in self.samples]
        errors = [error.copy() for error in self.errors]
        own = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        children = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        return {"scope": "controller process plus child max RSS and task-attributed Docker snapshots",
            "sample_interval_seconds": self.interval, "samples": samples,
            "sampled_peak_output_bytes": max((s["output_bytes"] for s in samples if "output_bytes" in s), default=None),
            "sampled_peak_container_memory_bytes": max(
                (s["container_memory_bytes"] for s in samples if s.get("container_count", 0)), default=None),
            "controller_maxrss_kib_linux": own, "child_maxrss_kib_linux": children,
            "container_true_peak_available": False, "errors": errors}
