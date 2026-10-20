"""Separate returned settings and token accounting from task correctness."""

from collections import Counter
import json

from .config import validate_generation_settings, validate_provider_policy
from .route_profile import IMAGE_DECLARATION, ROUTE_POLICY, BATCH_ROUTE_POLICY
from .messages import parse_response


SUMMARY_OUTPUT_CAP_POLICY = "completed_summary_output_cap_violation_v1"
SUMMARY_OUTPUT_CAP_KEY = "summary_output_cap_policy"


def summary_output_cap_violation(request, raw, config, check, role):
    """A completed summary reported over its wire cap, not an incomplete generation."""
    if (role != "compaction" or check.get("stop_reason") is not None
            or request.get("tools") != [] or request.get("tool_choice") != "none"
            or type(request.get("max_output_tokens")) is not int
            or request["max_output_tokens"] != config.max_output_tokens
            or raw.get("status") != "completed" or raw.get("incomplete_details") is not None):
        return False
    calls, usage = parse_response(raw, config.model)
    return (not calls and usage["input_tokens"] <= config.max_input_tokens
            and usage["output_tokens"] > config.max_output_tokens
            and all(item.get("status") in {None, "completed"} for item in raw["output"])
            and not any(part.get("type") == "refusal" for item in raw["output"]
                        if item.get("type") == "message"
                        for part in item.get("content", [])))


def check_summary_output_cap(request, raw, config, check, role, metadata):
    """Revalidate a local event version; never infer it for an unmarked old response."""
    if SUMMARY_OUTPUT_CAP_KEY not in metadata:
        return check
    if (metadata[SUMMARY_OUTPUT_CAP_KEY] != SUMMARY_OUTPUT_CAP_POLICY
            or metadata.get("kind") != "external"
            or type(metadata.get("http_status")) is not int or metadata["http_status"] != 200
            or type(metadata.get("transport_attempt_count")) is not int
            or metadata["transport_attempt_count"] != 1
            or not summary_output_cap_violation(request, raw, config, check, role)):
        raise ValueError("Recorded summary output-cap policy disagrees with response evidence")
    return {**check, "stop_reason": "provider_contract_error", "response_policy": {
        "version": SUMMARY_OUTPUT_CAP_POLICY, "role": role,
        "requested_output_cap": request["max_output_tokens"],
        "reported_output_tokens": raw["usage"]["output_tokens"],
    }}


def recorded_summary_output_metadata(event):
    """Restore original HTTP metadata only for the explicitly versioned event."""
    metadata = event.get("transport", {})
    if SUMMARY_OUTPUT_CAP_KEY not in metadata:
        return None
    if (event.get("role") != "compaction" or event.get("status") != "provider_contract_error"
            or event.get("accepted") is not False):
        raise ValueError("Invalid recorded summary output-cap event")
    return dict(metadata)


def _schema(value, key=None):
    if isinstance(value, dict):
        return {k: _schema(v, k) for k, v in value.items()
                if not (key is None and k == "output_schema" and v is None)}
    if isinstance(value, list):
        rows = [_schema(v, "element") for v in value]
        return sorted(rows) if key in {"required", "enum"} and all(isinstance(v, str) for v in rows) else rows
    return value


def check_provider_response(request: dict, response: dict, policy="strict", instruction_template=None) -> dict:
    validate_provider_policy(policy, instruction_template)
    conflicts, unconfirmed, usage_issues, usage_warnings = [], [], [], []
    effort = request.get("reasoning", {}).get("effort")
    try:
        validate_generation_settings(request["model"], effort)
    except ValueError:
        conflicts.append("unsupported_requested_reasoning_effort")
    reasoning = response.get("reasoning")
    if reasoning is not None and not isinstance(reasoning, dict):
        conflicts.append("malformed_reasoning_metadata")
    reported_effort = reasoning.get("effort") if isinstance(reasoning, dict) else None
    if reported_effort is None:
        unconfirmed.append("reasoning_effort")
    elif reported_effort != effort:
        conflicts.append("reasoning_effort")
    for key in ("model", "max_output_tokens", "parallel_tool_calls", "tool_choice", "store", "truncation", "previous_response_id"):
        actual, expected = response.get(key), request.get(key)
        if actual is None:
            if expected is not None:
                unconfirmed.append(key)
        elif actual != expected:
            conflicts.append(key)
    instructions = response.get("instructions")
    expected_instructions = [request.get("instructions")]
    expected_instructions += [m.get("content") for m in request.get("input", [])
                              if isinstance(m, dict) and m.get("role") in {"system", "developer"}]
    if instructions and instructions not in expected_instructions:
        conflicts.append("unexpected_response_instructions")
    template_matches = False
    template_normalization = "none_exact_string"
    template_envelope = None
    allowlist_present = (policy in {"allowlisted_instruction_template", ROUTE_POLICY, BATCH_ROUTE_POLICY}
                         and instructions not in (None, "") and instructions not in expected_instructions)
    if allowlist_present:
        template_matches = isinstance(instructions, str) and instructions == instruction_template
        if policy == "allowlisted_instruction_template" and isinstance(instructions, str) and not template_matches:
            normalized = instructions.replace("\r\n", "\n")
            template = instruction_template.replace("\r\n", "\n")
            template_matches = normalized == template
            if not template_matches:
                # Only an instruction already sent by this request may precede the approved template.
                template_matches = any(isinstance(prefix, str) and bool(prefix) and
                    normalized == prefix.replace("\r\n", "\n") + "\n\n" + template
                    for prefix in expected_instructions)
                if template_matches:
                    template_envelope = "request_instruction_then_template"
            if template_matches:
                template_normalization = "CRLF_to_LF_only"
        if not template_matches:
            conflicts.append("allowlisted_instruction_template_mismatch")
    if policy == "fixed_instruction_template":
        template_matches = (isinstance(instructions, str)
                            and instructions.replace("\r\n", "\n") == instruction_template.replace("\r\n", "\n"))
        if not template_matches:
            conflicts.append("fixed_instruction_template_mismatch")
    if response.get("tools") is None:
        unconfirmed.append("tools")
    else:
        tools = response["tools"]
        if not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools):
            conflicts.append("tools")
        elif sorted((_schema(t) for t in tools), key=lambda t: json.dumps(t, sort_keys=True)) != sorted(
                (_schema(t) for t in request.get("tools", [])), key=lambda t: json.dumps(t, sort_keys=True)):
            conflicts.append("tools")

    usage = response["usage"]
    reported = {k: usage[k] for k in ("input_tokens", "output_tokens", "total_tokens")}
    for parent, key, limit in (("input_tokens_details", "cached_tokens", "input_tokens"),
                               ("input_tokens_details", "cache_write_tokens", "input_tokens"),
                               ("output_tokens_details", "reasoning_tokens", "output_tokens")):
        details = usage.get(parent)
        if details is not None and not isinstance(details, dict):
            usage_issues.append("invalid_" + parent)
        elif isinstance(details, dict) and key in details:
            value = details[key]
            if type(value) is not int or not 0 <= value <= reported[limit]:
                usage_issues.append("invalid_" + key)
            else:
                reported[key] = value
    attribution = usage.get("attribution")
    totals, attribution_matches = None, None
    if attribution is not None:
        groups = [attribution.get(k) for k in ("items", "request_fields")] if isinstance(attribution, dict) else []
        if len(groups) != 2 or any(not isinstance(g, dict) for g in groups):
            usage_warnings.append("unreadable_usage_attribution")
        else:
            entries = [row for group in groups for row in group.values()]
            if any(not isinstance(row, dict) or any(type(row.get(k)) is not int or row[k] < 0
                   for k in ("input_tokens", "output_tokens")) for row in entries):
                usage_warnings.append("unreadable_usage_attribution")
            elif not entries:
                usage_warnings.append("empty_usage_attribution")
            else:
                totals = {k: sum(row[k] for row in entries) for k in ("input_tokens", "output_tokens")}
                attribution_matches = all(reported[k] == totals[k] for k in totals)
                if not attribution_matches:
                    usage_issues.append("usage_attribution_disagrees")
    calls = [item for item in response.get("output", []) if item.get("type") == "function_call"]
    tolerated = (policy == "observed_serial" and set(conflicts) == {"parallel_tool_calls"}
                 and request.get("parallel_tool_calls") is False and response.get("parallel_tool_calls") is True
                 and "tools" not in unconfirmed and len(calls) == 1)
    if policy == "fixed_instruction_template" or allowlist_present:
        tolerated = template_matches and set(conflicts) <= {"unexpected_response_instructions"}
    permitted = set()
    image_matches = False
    if policy in {ROUTE_POLICY, BATCH_ROUTE_POLICY}:
        reported_tools = response.get("tools")
        if isinstance(reported_tools, list) and all(isinstance(t, dict) for t in reported_tools):
            images = [t for t in reported_tools if t.get("type") == "image_generation"]
            remaining = [t for t in reported_tools if t.get("type") != "image_generation"]
            canonical = lambda values: sorted(json.dumps(_schema(t), sort_keys=True) for t in values)
            original_tools_match = canonical(remaining) == canonical(request.get("tools", []))
            image_matches = (json.dumps(images, sort_keys=True) ==
                             json.dumps([IMAGE_DECLARATION], sort_keys=True) and original_tools_match)
            intact_tools = original_tools_match and (not images or image_matches)
            if image_matches:
                permitted.add("tools")
            if policy == ROUTE_POLICY and intact_tools and request.get("parallel_tool_calls") is False and response.get("parallel_tool_calls") is True:
                permitted.add("parallel_tool_calls")
        if template_matches:
            permitted.add("unexpected_response_instructions")
        tolerated = bool(conflicts) and set(conflicts) <= permitted
    result = {
        "configuration": {"conflicts": sorted(set(conflicts)), "unconfirmed": sorted(set(unconfirmed)),
            "requested_reasoning_effort": effort, "reported_reasoning_effort": reported_effort,
            "response_instructions_chars": len(instructions) if isinstance(instructions, str) else None},
        "usage": {"reported": reported, "attribution_sum": totals,
            "attribution_matches": attribution_matches, "issues": sorted(set(usage_issues)),
            "warnings": sorted(set(usage_warnings)),
            "status": "conflicting" if usage_issues else
                      "provider_reported_consistent" if attribution_matches else "provider_reported_unchecked"},
        "request_size": {"message_count": len(request.get("input", [])),
            "wire_json_bytes": len(json.dumps(request, ensure_ascii=True).encode("utf-8"))},
        "reasoning_continuity": {
            "requested_encrypted_content": "reasoning.encrypted_content" in request.get("include", []),
            "returned_items": sum(item.get("type") == "reasoning" for item in response.get("output", [])),
            "items_without_encrypted_content": sum(item.get("type") == "reasoning" and not item.get("encrypted_content")
                                                   for item in response.get("output", [])),
            "policy": "preserve all returned output items in order; opaque reasoning is not interpreted as public task evidence",
        },
        "stop_reason": "provider_contract_error" if conflicts and not tolerated else
                       "provider_measurement_error" if usage_issues else None,
    }
    if policy != "strict" and (policy != "allowlisted_instruction_template" or allowlist_present):
        result["policy"] = policy
        result["continued_with_configuration_warning"] = tolerated and not usage_issues
    if policy == "fixed_instruction_template":
        result["instruction_template"] = {"matches": template_matches,
            "normalization": "CRLF_to_LF_only", "actual_model_injection": "unverified"}
    if allowlist_present:
        result["instruction_template"] = {"matches": template_matches, "present": True,
            "normalization": template_normalization, "actual_model_injection": "unverified"}
        if template_envelope:
            result["instruction_template"]["envelope"] = template_envelope
    if policy in {ROUTE_POLICY, BATCH_ROUTE_POLICY}:
        result["route_profile"] = {"exact_image_declaration_matches": image_matches,
            "permitted_conflicts": sorted(set(conflicts) & permitted),
            "blocking_conflicts": sorted(set(conflicts) - permitted),
            "execution_policy": ("ordered_original_functions_local_concurrency_one" if policy == BATCH_ROUTE_POLICY
                                 else "zero_or_one_original_function_only"),
            "upstream_execution": "unverified"}
    return result


def response_status(raw: dict, config, check: dict | None = None) -> str:
    if check and check["stop_reason"]:
        return check["stop_reason"]
    usage = raw["usage"]
    if usage["input_tokens"] > config.max_input_tokens:
        return "context_limit"
    if usage["output_tokens"] > config.max_output_tokens:
        return "output_limit"
    if raw["status"] == "incomplete":
        reason = (raw.get("incomplete_details") or {}).get("reason")
        return "incomplete_output" if reason == "max_output_tokens" else "incomplete_response"
    return "completed"


def retryable_configuration_response(raw: dict, config, check: dict) -> bool:
    policy = check.get("response_policy")
    if policy is not None:
        expected = {"version": SUMMARY_OUTPUT_CAP_POLICY, "role": "compaction",
                    "requested_output_cap": config.max_output_tokens,
                    "reported_output_tokens": raw["usage"]["output_tokens"]}
        original = {k: v for k, v in check.items() if k != "response_policy"}
        original["stop_reason"] = None
        request = {"tools": [], "tool_choice": "none", "max_output_tokens": config.max_output_tokens}
        return (policy == expected and check.get("stop_reason") == "provider_contract_error"
                and not check["usage"]["issues"]
                and summary_output_cap_violation(request, raw, config, original, "compaction"))
    conflicts = set(check["configuration"]["conflicts"])
    issues = set(check["usage"]["issues"])
    # Reject changed templates, but let the unchanged request use its bounded retry window.
    allowed = {"unexpected_response_instructions", "parallel_tool_calls", "tools",
               "allowlisted_instruction_template_mismatch", "fixed_instruction_template_mismatch"}
    allowed_issues = set()
    if config.retry_unrequested_linkage and isinstance(raw.get("previous_response_id"), str):
        allowed.add("previous_response_id")
    if config.provider_contract_retry_scope in {"settings_and_attribution", "settings_attribution_and_output_limit"}:
        allowed.add("reasoning_effort")
        allowed_issues.add("usage_attribution_disagrees")
    if config.provider_contract_retry_scope == "settings_attribution_and_output_limit":
        allowed.add("max_output_tokens")
    attribution = check["usage"].get("attribution_sum")
    if attribution is not None and (attribution["input_tokens"] > config.max_input_tokens
                                   or attribution["output_tokens"] > config.max_output_tokens):
        return False
    return bool(conflicts or issues) and conflicts <= allowed and issues <= allowed_issues and (
        check["stop_reason"] in {"provider_contract_error", "provider_measurement_error"} and
        raw["status"] == "completed" and raw["usage"]["input_tokens"] <= config.max_input_tokens
        and raw["usage"]["output_tokens"] <= config.max_output_tokens)


def checked_response_status(run: dict, event: dict, request: dict, response: dict, config) -> str:
    check = check_provider_response(request, response, config.provider_policy, config.provider_instruction_template)
    check = check_summary_output_cap(request, response, config, check, event.get("role"),
                                     event.get("transport", {}))
    if event.get("provider_check") != check:
        raise ValueError("Returned configuration/usage check differs from raw evidence")
    return response_status(response, config, check)


def budget_usage(usage, check, config):
    """Keep raw accounting intact; use the larger reported total for retry budgets."""
    if usage is None or not check or config.provider_contract_retry_scope not in {"settings_and_attribution", "settings_attribution_and_output_limit"}:
        return usage
    attribution = check["usage"].get("attribution_sum")
    if attribution is None:
        return usage
    return {**usage, "total_tokens": max(usage["total_tokens"], sum(attribution.values()))}


def summarize_provider_checks(events: list[dict]) -> dict:
    checks = [e["provider_check"] for e in events if "provider_check" in e]
    retry_requests = sum(bool(previous.get("configuration_retry")) for previous in events[:-1])
    rejection_retries = sum(bool(previous.get("prompt_rejection_retry")) for previous in events[:-1])
    return {"checked_responses": len(checks),
        "configuration_retry_requests": retry_requests,
        "configuration_retries_scheduled": sum(bool(e.get("configuration_retry")) for e in events),
        "prompt_rejection_retry_requests": rejection_retries,
        "prompt_rejection_retries_scheduled": sum(bool(e.get("prompt_rejection_retry")) for e in events),
        "continued_configuration_warning_responses": sum(bool(c.get("continued_with_configuration_warning")) for c in checks),
        "first_configuration_warning_turn": next((e["turn"] for e in events if e.get("provider_check", {}).get("continued_with_configuration_warning")), None),
        "evidence_class": "openlux_declared_image_ordered_batch" if any(c.get("policy") == BATCH_ROUTE_POLICY for c in checks)
                          else "openlux_declared_image_observed_serial" if any(c.get("policy") == ROUTE_POLICY for c in checks)
                          else "allowlisted_instruction_template_with_unverified_injection" if any(c.get("policy") == "allowlisted_instruction_template" for c in checks)
                          else "fixed_instruction_template_with_unverified_injection" if any(c.get("policy") == "fixed_instruction_template" for c in checks)
                          else "observed_serial_with_configuration_warning" if any(c.get("continued_with_configuration_warning") for c in checks)
                          else "prompt_rejection_retry_protocol" if rejection_retries
                          else "configuration_retry_protocol" if retry_requests else "no_continued_configuration_conflict",
        "configuration_conflict_responses": sum(bool(c["configuration"]["conflicts"]) for c in checks),
        "usage_conflict_responses": sum(bool(c["usage"]["issues"]) for c in checks),
        "configuration_conflicts": dict(Counter(k for c in checks for k in c["configuration"]["conflicts"])),
        "unconfirmed_fields": dict(Counter(k for c in checks for k in c["configuration"]["unconfirmed"])),
        "usage_issues": dict(Counter(k for c in checks for k in c["usage"]["issues"])),
        "usage_warnings": dict(Counter(k for c in checks for k in c["usage"]["warnings"])),
        "context_measurement_basis": "provider-reported tokens; attribution checks are not independent tokenizer verification"}
