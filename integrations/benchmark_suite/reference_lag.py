"""Public reference mentions and exposure, not inferred semantic dependency use."""

import json
from pathlib import Path

from skillhorizon.experiment.history_measurement import read_measurement, strict_rows, tokens, measure_ledger


def string_values(value):
    if isinstance(value, str):
        return {value}
    if isinstance(value, dict):
        return set().union(*(string_values(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(string_values(v) for v in value)) if value else set()
    return set()


def decoded(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return None
    return value


def measure(rows, record_ids):
    validation = measure_ledger(rows)
    if validation["status"] != "available":
        raise ValueError("Reference-use diagnostics require a validated public ledger")
    known = set(record_ids)
    offset = tokens(rows[0]["tools"])
    positions, calls, occurrences, uses = {}, {}, {}, []
    ignored_outputs = unparsed = 0
    for row in rows:
        if row["kind"] != "admission":
            continue
        message = row["message"] or {}
        size = tokens(message) if row["counted"] else 0
        positions[row["seq"]] = (offset, offset + size)
        offset += size
        if message.get("type") == "function_call":
            calls[message["call_id"]] = message["name"]
        elif message.get("type") == "function_call_output":
            if calls.get(message["call_id"]) == "read_skill_file":
                ignored_outputs += 1
                continue
            output = decoded(message["output"])
            if output is None:
                unparsed += 1
            for key in known & string_values(output):
                occurrences.setdefault(key, []).append(row["seq"])
    exposed, active = set(), set()
    for row in rows:
        if row["kind"] == "exposure" and row["role"] == "actor":
            active = set(row["event_ids"])
            exposed.update(active)
        if row["kind"] != "admission" or row["origin"] != "actor":
            continue
        message = row["message"] or {}
        if message.get("type") != "function_call":
            continue
        for key in sorted(known & string_values(decoded(message["arguments"]))):
            observations = occurrences.get(key, [])
            origin = observations[0] if observations else None
            eligible = next((event for event in observations if event < row["seq"] and event in exposed), None)
            visible = eligible is not None
            uses.append({"record_id": key, "argument_event": row["seq"], "first_observation_event": origin,
                "first_exposed_observation_event": eligible,
                "status": "exposed_argument_use" if visible else "not_directly_exposed" if origin is not None else "not_observed",
                "raw_token_gap": positions[row["seq"]][0] - positions[eligible][1] if visible else None,
                "original_in_latest_actor_exposure": origin in active if visible else None,
                "eligible_observation_in_latest_actor_exposure": eligible in active if visible else None})
    return {"metric": "first_public_record_reference_observation_to_exposed_argument_use",
            "known_committed_records": len(known), "records_observed": len(occurrences),
            "records_used_as_exact_argument_values": len({r["record_id"] for r in uses}),
            "exposed_argument_uses": sum(r["status"] == "exposed_argument_use" for r in uses),
            "ignored_skill_file_outputs": ignored_outputs, "unparsed_tool_outputs": unparsed,
            "uses": uses, "coordinate": "first eligible exposed observation end to argument start; uncompressed admissions; schema once; summaries excluded",
            "limitations": ["ID mention is not proof of production or semantic consumption",
                "Object keys, embedded code and implicit run bindings are not parsed",
                "Unsurfaced PTC child calls are excluded", "Same-batch future results are not prior exposure",
                "Historical exposure does not establish retention after compaction"]}


def from_episode(root, record_ids):
    if read_measurement(root)["status"] != "available":
        raise ValueError("Ledger ancestry is unavailable")
    return measure(strict_rows(Path(root) / "PUBLIC_HISTORY.jsonl"), record_ids)
