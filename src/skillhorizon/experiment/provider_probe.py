"""Bounded synthetic transport probes. No MCP, model-selected tool dispatch or task history."""

from copy import deepcopy
import time

from .context import input_size
from .messages import accountable_usage, parse_response, request_payload
from .provider_contract import check_provider_response, response_status, budget_usage
from .provider_diagnostics import diagnose_response
from .transport import ProviderFailure
from ..util import write_json

MAX_REQUESTS = 4
MAX_REPORTED_TOKENS = 50000
MAX_INPUT_TOKENS = 8192


def evaluate_probe_response(condition, payload, raw, config):
    calls, usage = parse_response(raw, config.model)
    check = check_provider_response(payload, raw, config.provider_policy)
    status = response_status(raw, config, check)
    confirmed = not {"model", "tools", "parallel_tool_calls", "reasoning_effort"}.intersection(
        check["configuration"]["unconfirmed"])
    text = "".join(c.get("text", "") for item in raw["output"] if item["type"] == "message"
                   for c in item["content"] if c["type"] == "output_text").strip()
    expected_output = (len(calls) == 1 and calls[0]["name"] == "read_project"
                       and calls[0]["arguments"] == {}) if condition == "mcp_schema" else (
                           not calls and text == "ROUTE_PROBE_OK")
    return {"provider_check": check, "status": status,
            "required_metadata_confirmed": confirmed, "expected_output": expected_output,
            "passed": status == "completed" and confirmed and expected_output}, usage


def probe_payloads(config, tools):
    if config.provider_policy != "strict":
        raise ValueError("Route qualification requires strict configuration checks")
    if not any(t.get("name") == "read_project" and t.get("type") == "function" for t in tools):
        raise ValueError("Probe requires the public benchmark read_project schema")
    payloads = []
    for _ in range(2):
        plain = request_payload(config, [{"role": "user", "content":
            "Synthetic transport diagnostic. Reply only ROUTE_PROBE_OK."}], [])
        plain["tool_choice"] = "none"
        actor = request_payload(config, [{"role": "user", "content":
            "Synthetic benchmark transport diagnostic, not a real project. "
            "Call read_project once with empty arguments. Do not do anything else."}], tools)
        payloads.extend([("no_tools", plain), ("mcp_schema", actor)])
    if any(input_size(p) > MAX_INPUT_TOKENS for _, p in payloads):
        raise ValueError("Synthetic probe input exceeds the small-request cap")
    return payloads


def run_probe(root, config, transport, tools):
    payloads = probe_payloads(config, tools)
    root.mkdir(parents=True, exist_ok=False)
    write_json(root / "PLAN.json", {"purpose": "Small synthetic route qualification; not behavior evidence",
        "config": config.to_dict(), "public_tools": tools, "max_requests": MAX_REQUESTS,
        "reported_token_stop_threshold": MAX_REPORTED_TOKENS, "retries": 0,
        "tool_execution": False, "task_history_sent": False,
        "note": "Post-response threshold, not currency cap. Passing cannot certify future routing."})
    rows, tokens = [], 0
    for number, (condition, payload) in enumerate(payloads, 1):
        if tokens >= MAX_REPORTED_TOKENS:
            break
        directory = root / "requests" / f"{number:04d}"
        write_json(directory / "request.json", payload)
        row = {"request": number, "condition": condition, "passed": False,
               "local_input_tokens": input_size(payload), "reported_tokens": None}
        raw, check, usage = None, None, None
        try:
            before = deepcopy(payload)
            raw, metadata = transport.send(payload)
            write_json(directory / "response.json", raw)
            if payload != before:
                raise ValueError("Transport changed probe request")
            row["transport"] = metadata
            row["diagnostic"] = diagnose_response(payload, raw)
            checked, usage = evaluate_probe_response(condition, payload, raw, config)
            row.update(checked)
            check = checked["provider_check"]
        except ProviderFailure as exc:
            row.update(status="provider_error", error=exc.to_dict())
        except (ValueError, TypeError, KeyError):
            row["status"] = "invalid_response"
            usage = accountable_usage(raw)
        charged = budget_usage(usage, check, config)
        if charged:
            row["reported_tokens"] = charged["total_tokens"]
            tokens += charged["total_tokens"]
        rows.append(row)
        write_json(directory / "RESULT.json", row)
        write_json(root / "PROGRESS.json", {"requests": len(rows), "known_tokens": tokens})
        if row.get("error", {}).get("details", {}).get("http_status") in {401, 403}:
            break
    report = {"passed": len(rows) == MAX_REQUESTS and all(r["passed"] for r in rows),
        "requests": len(rows), "known_tokens": tokens,
        "unknown_usage_requests": sum(r["reported_tokens"] is None for r in rows),
        "tool_calls_executed": 0, "rows": rows,
        "completed_at": time.time(),
        "upstream_cause": "unverified", "formal_behavior_evidence": False,
        "meaning": "Sampled transport compatibility only; retain per-response checks even after a pass."}
    write_json(root / "RESULT.json", report)
    return report
