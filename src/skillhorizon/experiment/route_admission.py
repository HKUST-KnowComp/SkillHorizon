"""One public-schema probe before an expensive native model trajectory."""

from copy import deepcopy
from dataclasses import replace
from pathlib import Path

from .context import input_size
from .messages import accountable_usage, parse_response, request_payload
from .mcp_tools import strict_compatible_mcp_schema
from .provider_contract import check_provider_response, response_status
from .provider_diagnostics import diagnose_response
from .transport import ProviderFailure
from ..util import write_json

ADMISSION_POLICY = {"version": "public_schema_probe_v1", "max_provider_requests": 1,
                    "max_input_tokens": 32768, "max_output_tokens": 1024,
                    "timeout_seconds": 180, "retries": 0, "tool_execution": False}


def probe_config(config):
    return replace(config, context_policy="raw", max_output_tokens=1024,
                   max_turns=1, max_provider_requests=1, max_total_tokens=33792,
                   timeout_seconds=min(180, config.timeout_seconds), provider_contract_retries=0,
                   rate_limit_retries=0, transport_retries=0, prompt_rejection_retries=0)


def probe_payload(config, tools):
    request = request_payload(probe_config(config), [{"role": "user", "content":
        "Reply with OK. This is a synthetic API schema-admission check. Do not call tools."}], tools)
    request["tool_choice"] = "none"
    return request


def run_admission(tools, config, transport, output):
    root = Path(output)
    root.mkdir(parents=True, exist_ok=False)
    request = probe_payload(config, tools)
    write_json(root / "request.json", request)
    report = {"passed": False, "provider_calls": 0, "tool_calls_executed": 0,
              "business_evidence": False, "policy": deepcopy(ADMISSION_POLICY),
              "reported_usage": None, "usage_unknown": False}
    invalid = [t.get("name") for t in tools if t.get("strict") is True
               and not strict_compatible_mcp_schema(t.get("parameters"))]
    if invalid:
        report.update(status="invalid_frozen_strict_schema", tools=invalid)
    elif input_size(request) > ADMISSION_POLICY["max_input_tokens"]:
        report["status"] = "probe_input_limit"
    else:
        raw = None
        report["provider_calls"] = 1
        try:
            before = deepcopy(request)
            raw, metadata = transport.send(request)
            if request != before:
                raise ValueError("Transport changed admission request")
            write_json(root / "response.json", raw)
            write_json(root / "transport.json", metadata)
            calls, _ = parse_response(raw, config.model)
            check = check_provider_response(request, raw, config.provider_policy, config.provider_instruction_template)
            status = response_status(raw, probe_config(config), check)
            missing = sorted(set(check["configuration"]["unconfirmed"]) &
                             {"reasoning_effort", "tools", "parallel_tool_calls", "tool_choice"})
            missing_warning = (config.provider_policy == "allowlisted_instruction_template"
                               and check["stop_reason"] is None
                               and not check["usage"]["issues"])
            if status == "completed":
                if calls:
                    status = "probe_unrequested_tool_call"
                elif missing and not missing_warning:
                    status = "probe_critical_configuration_unconfirmed"
                elif raw["usage"]["input_tokens"] > ADMISSION_POLICY["max_input_tokens"]:
                    status = "probe_input_limit"
            report.update(status=status, passed=status == "completed", provider_check=check,
                          critical_unconfirmed=missing)
            if status == "completed" and missing and missing_warning:
                # Missing echoes do not confirm actual settings or contradict requested labels.
                report.update(warnings=["probe_critical_configuration_unconfirmed"],
                              provider_policy=config.provider_policy,
                              reported_setting_confirmation="unconfirmed")
        except ProviderFailure as exc:
            report.update(status="provider_error", error=exc.to_dict())
            write_json(root / "error.json", exc.to_dict())
        except (ValueError, TypeError, KeyError):
            report["status"] = "invalid_probe_response"
        if isinstance(raw, dict):
            report["configuration_diagnostics"] = diagnose_response(request, raw)
        report["reported_usage"] = accountable_usage(raw)
        report["usage_unknown"] = report["reported_usage"] is None
    write_json(root / "RESULT.json", report)
    return report
