"""Evidence-only provider diagnostics; never rewrites responses or grants dispatch."""

from collections import Counter
import json

from .provider_contract import _schema


def _tool_key(tool):
    return (str(tool.get("type")), str(tool.get("name", "")))


def tool_delta(requested, reported):
    if reported is None:
        return {"status": "unreported"}
    if not isinstance(reported, list) or any(not isinstance(t, dict) for t in reported):
        return {"status": "malformed"}
    def groups(tools):
        result = {}
        for tool in tools:
            result.setdefault(_tool_key(tool), []).append(json.dumps(_schema(tool), sort_keys=True))
        return result
    expected, actual = groups(requested), groups(reported)
    def labels(keys):
        return [{"type": key[0], "name": key[1] or None} for key in sorted(keys)]
    return {"status": "reported", "added": labels(actual.keys() - expected.keys()),
            "missing": labels(expected.keys() - actual.keys()),
            "changed": labels(k for k in actual.keys() & expected.keys()
                              if sorted(actual[k]) != sorted(expected[k])),
            "duplicates": labels(k for k, rows in actual.items() if len(rows) > 1)}


def diagnose_response(request, response):
    """Report observable fields only; metadata cannot prove the upstream model input."""
    if not isinstance(response, dict):
        return {"response_shape": "malformed", "upstream_cause": "unverified"}
    output = response.get("output")
    output = output if isinstance(output, list) else []
    items = [item for item in output if isinstance(item, dict)]
    calls = [item for item in items if item.get("type") == "function_call"]
    allowed = {t.get("name") for t in request.get("tools", []) if t.get("type") == "function"}
    delta = tool_delta(request.get("tools", []), response.get("tools"))
    extra = [item.get("type") for item in items
             if item.get("type") not in {"reasoning", "message", "function_call"}]
    return {"tool_delta": delta,
            "parallel_tool_calls": {"requested": request.get("parallel_tool_calls"),
                                    "reported": response.get("parallel_tool_calls")},
            "observed_output": {"function_call_count": len(calls),
                "function_names": [item.get("name") for item in calls],
                "unknown_function_names": sorted({str(item.get("name")) for item in calls
                                                  if item.get("name") not in allowed}),
                "unsupported_item_types": dict(Counter(map(str, extra))),
                "multiple_function_calls": len(calls) > 1},
            "upstream_cause": "unverified",
            "scope": "Reported configuration versus observed output only. Not argument validation, "
                     "execution permission, or proof of harmless response wrapping."}
