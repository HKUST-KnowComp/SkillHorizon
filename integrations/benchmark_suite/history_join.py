"""Join public-history admissions to the actual request, response and tool logs."""

import json
from pathlib import Path

from skillhorizon.experiment.history_measurement import projection
from skillhorizon.experiment.messages import canonical_history
from skillhorizon.util import load_json, load_jsonl


def verify(run, bundle, rows):
    run, bundle = Path(run), Path(bundle)
    public = load_json(bundle / "public/task.json")
    initial = load_json(bundle / "public/history.json")
    admissions = [r for r in rows if r["kind"] == "admission"]
    if (rows[0]["tools"] != public["tools"] or
            [r["message"] for r in admissions if r["attempt"] == 0] != [projection(m) for m in initial]):
        raise ValueError("Ledger schema/initial history does not belong to this run bundle")
    events = {r["seq"]: r["message"] for r in rows if r["kind"] in {"admission", "summary"}}
    for row in rows:
        directory = run / "turns" / f'{row.get("attempt", 0):04d}'
        if row["kind"] == "exposure":
            request = load_json(directory / "request.json")
            if row["role"] == "actor" and ([events[i] for i in row["event_ids"]] != [projection(m) for m in request["input"]]
                                           or request["tools"] != public["tools"]):
                raise ValueError("Ledger exposure differs from actual actor request")
        elif row["kind"] == "decision" and row["admitted"]:
            response = load_json(directory / "response.json")["output"]
            if row.get("reason") == "protocol_feedback":
                response = canonical_history(response)
            actual = [r["message"] for r in admissions if r["origin"] == "actor" and r["attempt"] == row["attempt"]]
            if actual != [projection(m) for m in response]:
                raise ValueError("Ledger actor admissions differ from actual provider response")
        elif row["kind"] == "summary_generation":
            if row["output"] != [projection(m) for m in load_json(directory / "response.json")["output"]]:
                raise ValueError("Ledger summary differs from its provider response")
    summary = load_json(run / "SUMMARY.json")
    records = []
    for filename, count in (("tools.jsonl", "executed_tool_calls"), ("tool_rejections.jsonl", "rejected_tool_calls"),
                            ("protocol_rejections.jsonl", "protocol_rejected_calls")):
        path = run / filename
        if summary[count] and not path.is_file():
            raise FileNotFoundError(path)
        part = load_jsonl(path)
        if len(part) != summary[count]:
            raise ValueError("Runtime tool-log count differs from run summary")
        records.extend(part)
    observed = {r["call_id"]: r for r in records}
    if len(observed) != len(records):
        raise ValueError("Duplicate tool identity across run logs")
    calls = {r["message"]["call_id"]: r for r in admissions if r["attempt"] > 0 and (r["message"] or {}).get("type") == "function_call"}
    results = {r["message"]["call_id"]: r for r in admissions if r["attempt"] > 0 and (r["message"] or {}).get("type") == "function_call_output"}
    if set(results) != set(observed) or set(calls) != set(observed) | set(summary["pending_tool_call_ids"]):
        raise ValueError("Ledger tool identities differ from completed and pending run calls")
    for key, record in observed.items():
        call, result = calls[key], results[key]
        args_equal = (call["message"]["arguments"] == record["raw_arguments"] if "raw_arguments" in record
                      else json.loads(call["message"]["arguments"]) == record["arguments"])
        if (call["message"]["name"] != record["name"] or not args_equal
                or call["attempt"] != record["turn"] or result["attempt"] != record["turn"]
                or json.loads(result["message"]["output"]) != record["output"]):
            raise ValueError("Ledger tool call/result differs from actual run effects")
    return {"passed": True, "joined_calls": len(observed)}
