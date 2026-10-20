"""Generic public Client transport installed by the native reference actor.

The file is copied into the agent-owned terminal during the first visible turn.
It contains no instance answers or evaluator access.
"""

import json
import os
from pathlib import Path
import sys


RESULT = Path("/tmp/skillhorizon-public-result.json")
BEGIN = "SH_PUBLIC_REFERENCE_BEGIN:"
DATA = "SH_PUBLIC_REFERENCE_DATA:"
END = "SH_PUBLIC_REFERENCE_END:"
READY = "SH_PUBLIC_REFERENCE_HELPER_READY"
CHUNK = 600
LINE_LIMIT = 76


def _pieces(part):
    result, offset = [], 0
    while offset < len(part):
        index = len(result)
        limit = offset
        while limit < len(part):
            candidate = json.dumps(part[offset:limit + 1], ensure_ascii=True)
            if len(f"{DATA}{index}:{candidate}") > LINE_LIMIT:
                break
            limit += 1
        if limit == offset:
            raise ValueError("Public result character exceeds terminal line")
        result.append(part[offset:limit])
        offset = limit
    return result


def page_lines(raw, ordinal, offset):
    if type(ordinal) is not int or type(offset) is not int or ordinal < 1 or not 0 <= offset < len(raw):
        raise ValueError("Invalid public result page")
    part = raw[offset:offset + CHUNK].decode("ascii")
    pieces = _pieces(part)
    return [f"{BEGIN}{ordinal}:{offset}:{len(raw)}:{len(pieces)}",
            *(f"{DATA}{i}:{json.dumps(piece, ensure_ascii=True)}" for i, piece in enumerate(pieces)),
            f"{END}{ordinal}:{offset}"]


def emit(ordinal, offset):
    raw = RESULT.read_bytes()
    record = json.loads(raw)
    if (not isinstance(record, dict) or set(record) != {"call", "payload"}
            or type(record["call"]) is not int or record["call"] != ordinal):
        raise ValueError("Stale public result file")
    for line in page_lines(raw, ordinal, offset):
        print(line, flush=True)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--ready"]:
        print(READY, flush=True)
        return
    if len(argv) == 3 and argv[0] == "call":
        from client import Client
        action = json.loads(argv[1])
        if (not isinstance(action, dict) or set(action) != {"name", "arguments"}
                or not isinstance(action["name"], str) or not isinstance(action["arguments"], dict)):
            raise ValueError("Invalid public action")
        ordinal = int(argv[2])
        if ordinal < 1:
            raise ValueError("Invalid public call identity")
        result = Client(timeout=120).call(action["name"], action["arguments"])
        raw = json.dumps({"call": ordinal, "payload": result}, ensure_ascii=True,
                         separators=(",", ":"), allow_nan=False).encode("ascii")
        temporary = RESULT.with_suffix(".tmp")
        temporary.write_bytes(raw)
        os.replace(temporary, RESULT)
        emit(ordinal, 0)
        return
    if len(argv) == 3 and argv[0] == "page":
        emit(int(argv[1]), int(argv[2]))
        return
    raise ValueError("Invalid public reference command")


if __name__ == "__main__":
    main()
