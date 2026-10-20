"""Host interpreter contract, independent of the execution guest runtime."""

import json
from pathlib import Path
import subprocess
import sys


MINIMUM_PYTHON = (3, 11)


class UnsupportedBenchmarkPython(RuntimeError):
    pass


def _require_version(version, executable):
    if tuple(version[:2]) < MINIMUM_PYTHON:
        required = ".".join(map(str, MINIMUM_PYTHON))
        actual = ".".join(map(str, version))
        raise UnsupportedBenchmarkPython(
            f"UNSUPPORTED_BENCHMARK_PYTHON: required>={required}; actual={actual}; executable={executable}"
        )


def require_supported_python():
    """Reject unsupported hosts before importing dependencies or touching a run."""
    _require_version(sys.version_info[:3], sys.executable)


def check_python_executable(executable):
    """Probe the explicit worker, without site initialization or PATH fallback."""
    require_supported_python()
    executable = str(executable)
    if not Path(executable).is_absolute():
        raise RuntimeError(f"BENCHMARK_PYTHON_PROBE_FAILED: absolute executable required; executable={executable}")
    probe = "import json,sys; print(json.dumps({'version':list(sys.version_info[:3]),'executable':sys.executable}))"
    try:
        result = subprocess.run(
            [executable, "-I", "-S", "-c", probe],
            capture_output=True, text=True, timeout=10, check=True,
        )
        identity = json.loads(result.stdout)
        if (not isinstance(identity, dict) or set(identity) != {"version", "executable"}
                or not isinstance(identity["version"], list) or len(identity["version"]) != 3
                or any(type(value) is not int or value < 0 for value in identity["version"])
                or not isinstance(identity["executable"], str)
                or not Path(identity["executable"]).is_absolute()):
            raise ValueError("Invalid interpreter identity")
    except (OSError, subprocess.SubprocessError, ValueError):
        raise RuntimeError(f"BENCHMARK_PYTHON_PROBE_FAILED: executable={executable}") from None
    _require_version(identity["version"], executable)
    return identity
