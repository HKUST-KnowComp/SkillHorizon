"""Bound controller-owned execution journals before JSON decoding or replay.

The caller supplies the limits derived by ExecutionWorkspace and must validate
the decoded records with that same ordered consumer. This is not a tool API.
"""

import json
import math
import os
from pathlib import Path
import stat

from .ptc_config import EPISODE_STORAGE_CEILING


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate execution journal key")
        result[key] = value
    return result


def _constant(value):
    raise ValueError("Non-finite execution journal value")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite execution journal value")
    return result


def read_execution_records(path, *, max_bytes, max_record_bytes, max_actions):
    for limit in (max_bytes, max_record_bytes, max_actions):
        if type(limit) is not int or limit <= 0:
            raise ValueError("Execution replay requires positive explicit read limits")
    # E1's per-parent worst case can exceed its episode envelope when the
    # episode permits fewer children than one program. Enforce both limits on
    # each read, without requiring their independently derived maxima to order.
    if max_bytes > EPISODE_STORAGE_CEILING or max_record_bytes > EPISODE_STORAGE_CEILING or max_actions > 100000:
        raise ValueError("Execution replay read limits exceed the supported envelope")
    fd = None
    try:
        fd = os.open(Path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > max_bytes:
            raise ValueError("Execution journal is unsafe or exceeds the declared byte limit")
        records, consumed = [], 0
        with os.fdopen(fd, "rb") as handle:
            fd = None
            while True:
                line = handle.readline(min(max_record_bytes, max_bytes - consumed) + 1)
                if not line:
                    break
                consumed += len(line)
                if (len(records) >= max_actions or len(line) > max_record_bytes or consumed > max_bytes
                        or not line.endswith(b"\n") or not line.strip()):
                    raise ValueError("Execution journal exceeds bounds or has an incomplete record")
                record = json.loads(line.decode("utf-8"), object_pairs_hook=_object,
                                    parse_constant=_constant, parse_float=_finite_float)
                if type(record) is not dict:
                    raise ValueError("Execution journal entries must be objects")
                records.append(record)
            after = os.fstat(handle.fileno())
            if (after.st_size, after.st_mtime_ns, after.st_ctime_ns, after.st_nlink) != (
                    info.st_size, info.st_mtime_ns, info.st_ctime_ns, 1) or consumed != info.st_size:
                raise ValueError("Execution journal changed during bounded collection")
        return records
    except (OSError, UnicodeError, RecursionError):
        raise ValueError("Execution journal cannot be safely decoded") from None
    finally:
        if fd is not None:
            os.close(fd)
