"""Offline correspondence audit of native public results against gateway results.

Gateway evidence is read only after execution; it never enters reference policy.
The report contains counts and validity, not business payloads.
"""

import argparse
import json
from pathlib import Path
import re
import shlex

from integrations.harbor.backend import write_record
from integrations.harbor.reference_fixture import FrameCollector
from integrations.harbor.reference_terminal_helper import BEGIN, DATA, END, READY

from .native_reference_length import business_length, serialize


_HEADER = re.compile(r"^SH_PUBLIC_REFERENCE_BEGIN:(\d+):(\d+):(\d+):(\d+)$")
_PART = re.compile(r"^SH_PUBLIC_REFERENCE_DATA:(\d+):(.+)$")
_FOOTER = re.compile(r"^SH_PUBLIC_REFERENCE_END:(\d+):(\d+)$")


def _command(value):
    if (value.startswith("cat > /tmp/skillhorizon_reference_helper.py <<'SH_PUBLIC_REFERENCE_HELPER'\n")
            and value.endswith("\npython /tmp/skillhorizon_reference_helper.py --ready\n")):
        return "setup", None
    parts = shlex.split(value)
    if not parts:
        raise ValueError("Empty native command")
    if len(parts) == 5 and parts[0] == "python" and parts[1].endswith("/skillhorizon_reference_helper.py"):
        if parts[2] == "call":
            action = _strict_json(parts[3])
            ordinal = int(parts[4])
            if (not isinstance(action, dict) or set(action) != {"name", "arguments"}
                    or not isinstance(action["name"], str) or not isinstance(action["arguments"], dict)):
                raise ValueError("Invalid reference public action")
            return "call", (ordinal, action)
        if parts[2] == "page":
            return "page", (int(parts[3]), int(parts[4]))
    raise ValueError("Unexpected native reference command")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key in native evidence")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError(f"Non-finite JSON value: {value}")


def _strict_json(value):
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)


def _visible_segments(text, issued, open_key, orphan):
    segments = {}
    for line in text.splitlines():
        line = line.rstrip("\r")
        if line.startswith(BEGIN):
            match = _HEADER.fullmatch(line)
            if match is None:
                raise ValueError("Malformed visible public result header")
            key = int(match[1]), int(match[2])
            if key not in issued or orphan or (open_key is not None and open_key != key):
                raise ValueError("Unissued or overlapping public result frame")
            open_key = key
            segments.setdefault(key, []).append(line)
        elif line.startswith(END):
            match = _FOOTER.fullmatch(line)
            if match is None:
                raise ValueError("Malformed visible public result footer")
            key = int(match[1]), int(match[2])
            if key not in issued:
                raise ValueError("Unissued public result footer")
            if open_key is None:
                collector = issued[key]
                if (not collector.ended or collector.total is None or
                        len(collector.parts) != collector.count or
                        any(collector.parts.get(index) != value for index, value in orphan)):
                    raise ValueError("Conflicting or incomplete replay suffix")
                orphan = []
            elif open_key != key:
                raise ValueError("Unmatched public result footer")
            else:
                segments.setdefault(key, []).append(line)
            open_key = None
        elif line.startswith(DATA):
            match = _PART.fullmatch(line)
            if match is None:
                raise ValueError("Malformed public result data")
            if open_key is None:
                value = _strict_json(match[2])
                if not isinstance(value, str) or not value or not value.isascii():
                    raise ValueError("Invalid replay suffix data")
                orphan.append((int(match[1]), value))
            else:
                segments.setdefault(open_key, []).append(line)
    return segments, open_key, orphan


def audit_events(visible, gateway):
    if (not visible or [entry.get("index") for entry in visible] != list(range(len(visible)))):
        raise ValueError("Invalid visible event sequence")
    first_response = next((i for i, entry in enumerate(visible)
                           if entry.get("origin") != "initial"), len(visible))
    if first_response < 1 or first_response == len(visible):
        raise ValueError("Missing initial messages or reference response")
    for i, entry in enumerate(visible[first_response:]):
        if entry.get("origin") != ("reference_response" if i % 2 == 0 else "native_observation"):
            raise ValueError("Unexpected native event order")
    business = business_length(gateway)
    actions, frames, issued = {}, {}, {}
    pending, active_call, setup_pending, setup_seen = None, None, False, False
    open_key, orphan = None, []
    page_commands = 0
    for entry in visible[first_response:]:
        if entry["origin"] == "native_observation":
            text = entry["message"]["content"]
            if setup_pending and READY in text.splitlines():
                setup_pending = False
            segments, open_key, orphan = _visible_segments(text, issued, open_key, orphan)
            for key, lines in segments.items():
                frame = issued[key].feed("\n".join(lines))
                if frame is not None:
                    ordinal, offset = key
                    pages = frames.setdefault(ordinal, {})
                    if offset in pages:
                        if pages[offset] != frame:
                            raise ValueError("Conflicting repeated public page")
                    else:
                        if key != pending:
                            raise ValueError("Unexpected completed public page")
                        pages[offset] = frame
                    if key == pending:
                        pending = None
                        seen_bytes = sum(len(item["chunk"]) for item in pages.values())
                        if seen_bytes == frame["total"]:
                            active_call = None
                        elif seen_bytes > frame["total"]:
                            raise ValueError("Public result page overflow")
            continue
        response = _strict_json(entry["message"]["content"])
        commands = response.get("commands")
        if not isinstance(commands, list) or len(commands) > 1:
            raise ValueError("Unexpected native command batch")
        if not commands:
            continue
        if pending is not None or setup_pending:
            raise ValueError("New command before previous terminal result")
        kind, value = _command(commands[0]["keystrokes"])
        if kind == "setup":
            if setup_seen or actions:
                raise ValueError("Duplicate or late helper setup")
            setup_seen = setup_pending = True
        elif kind == "call":
            ordinal, action = value
            if not setup_seen or active_call is not None or ordinal != len(actions) + 1:
                raise ValueError("Out-of-order business call")
            actions[ordinal] = action
            active_call = ordinal
            pending = ordinal, 0
            issued[pending] = FrameCollector(ordinal, 0)
        else:
            ordinal, offset = value
            pages = frames.get(ordinal, {})
            if (active_call != ordinal or not pages or
                    offset != sum(len(frame["chunk"]) for frame in pages.values())):
                raise ValueError("Out-of-order public result page")
            page_commands += 1
            pending = ordinal, offset
            issued[pending] = FrameCollector(ordinal, offset)
    if (pending is not None or active_call is not None or open_key is not None or orphan
            or setup_pending or not setup_seen):
        raise ValueError("Incomplete native terminal transport")
    if len(issued) != sum(len(pages) for pages in frames.values()):
        raise ValueError("Issued public page was not observed")
    if len(actions) != business["top_level_calls"] or len(gateway) != 2 * len(actions):
        raise ValueError("Native and gateway business call counts differ")
    for ordinal, action in actions.items():
        pages = frames.get(ordinal, {})
        if not pages or 0 not in pages:
            raise ValueError("Missing first public result page")
        total = pages[0]["total"]
        if any(frame["total"] != total for frame in pages.values()):
            raise ValueError("Public result total changed")
        raw, offset = bytearray(), 0
        while offset < total:
            frame = pages.get(offset)
            if frame is None:
                raise ValueError("Missing public result page")
            raw.extend(frame["chunk"])
            offset += len(frame["chunk"])
        record = _strict_json(raw.decode("ascii"))
        if (not isinstance(record, dict) or set(record) != {"call", "payload"}
                or type(record["call"]) is not int or record["call"] != ordinal):
            raise ValueError("Wrong terminal result identity")
        dispatch, result = gateway[2 * (ordinal - 1):2 * ordinal]
        if (dispatch.get("name") != action["name"]
                or serialize(dispatch.get("arguments")) != serialize(action["arguments"])
                or serialize(result.get("result")) != serialize(record["payload"])):
            raise ValueError("Public terminal and gateway result mismatch")
    return {"status": "passed", "public_calls": len(actions), "page_commands": page_commands,
            "terminal_pages": sum(len(pages) for pages in frames.values()),
            "visible_events": len(visible), "gateway_events": len(gateway),
            "all_actions_and_results_match": True,
            "scope": "postrun audit only; gateway never supplied to policy"}


def run(native_root, output):
    native_root, output = Path(native_root).resolve(), Path(output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    try:
        reference = _strict_json((native_root / "REFERENCE_LENGTH.json").read_text())
        if reference.get("status") != "passed":
            raise ValueError("Complete native reference not passed")
        gateway = Path(reference["trial_result"]).parent / "business_private/gateway.jsonl"
        visible = [_strict_json(line) for line in (native_root / "NATIVE_VISIBLE.jsonl").read_text().splitlines() if line]
        events = [_strict_json(line) for line in gateway.read_text().splitlines() if line]
        report = audit_events(visible, events)
    except Exception as error:
        report = {"status": "failed", "failure_type": type(error).__name__}
    write_record(output / "TRANSPORT_AUDIT.json", report)
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("native_root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = run(args.native_root, args.output)
    print(result, flush=True)
    if result["status"] != "passed":
        raise SystemExit(2)


if __name__ == "__main__":
    main()
