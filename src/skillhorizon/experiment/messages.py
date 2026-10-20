from copy import deepcopy
from typing import Any

from ..util import canonical_json
from .config import RunConfig
from .argument_feedback import parse_arguments


def canonical_history(messages: list[dict]) -> list[dict]:
    """Project protocol items, not provider-private tracking fields, onto the wire."""
    fields = {
        "message": {"type", "id", "role", "content", "status", "phase"},
        "reasoning": {"type", "id", "summary", "content", "encrypted_content", "status"},
        "function_call": {"type", "id", "call_id", "name", "arguments", "status"},
        "function_call_output": {"type", "id", "call_id", "output", "status"},
    }
    result = []
    for item in messages:
        kind = item.get("type", "message")
        if kind not in fields:
            raise ValueError("Unsupported history item type")
        result.append({key: deepcopy(value) for key, value in item.items() if key in fields[kind]})
    return result


def request_payload(config: RunConfig, messages: list[dict], tools: list[dict]) -> dict:
    return {
        "model": config.model,
        "input": canonical_history(messages) if config.canonical_history else deepcopy(messages),
        "tools": deepcopy(tools),
        "tool_choice": "auto",
        "parallel_tool_calls": config.parallel_tool_calls,
        "max_output_tokens": config.max_output_tokens,
        "reasoning": {"effort": config.reasoning_effort},
        "store": False,
        "truncation": "disabled",
        "include": ["reasoning.encrypted_content"],
    }


def accountable_usage(raw):
    """Recover internally consistent reported usage, never permission to use output."""
    usage = raw.get("usage") if isinstance(raw, dict) else None
    if (not isinstance(usage, dict) or any(type(usage.get(key)) is not int or usage[key] < 0
            for key in ("input_tokens", "output_tokens", "total_tokens"))
            or usage["input_tokens"] + usage["output_tokens"] != usage["total_tokens"]):
        return None
    result = {key: usage[key] for key in ("input_tokens", "output_tokens", "total_tokens")}
    for field, limit in (("input_tokens_details", "input_tokens"), ("output_tokens_details", "output_tokens")):
        details = usage.get(field)
        if isinstance(details, dict):
            result[field] = {key: value for key, value in details.items() if type(value) is int and 0 <= value <= usage[limit]}
    return result


def parse_response(raw: Any, model: str) -> tuple[list[dict], dict]:
    if not isinstance(raw, dict) or raw.get("model") != model:
        raise ValueError("Provider response model mismatch")
    if raw.get("status") not in {"completed", "incomplete"} or raw.get("error"):
        raise ValueError("Unsuccessful provider response")
    if raw.get("incomplete_details") is not None and not isinstance(raw["incomplete_details"], dict):
        raise ValueError("Malformed completion details")
    usage = raw.get("usage")
    if (
        not isinstance(usage, dict)
        or any(
            type(usage.get(k)) is not int or usage[k] < 0
            for k in ("input_tokens", "output_tokens")
        )
        or usage["input_tokens"] == 0
    ):
        raise ValueError("Invalid provider token usage")
    if (
        type(usage.get("total_tokens")) is not int
        or usage["total_tokens"] != usage["input_tokens"] + usage["output_tokens"]
    ):
        raise ValueError("Inconsistent total token usage")
    for field, limit, keys in (
        ("input_tokens_details", "input_tokens", ("cached_tokens", "cache_write_tokens")),
        ("output_tokens_details", "output_tokens", ("reasoning_tokens",)),
    ):
        if usage.get(field) is None:
            continue
        details = usage[field]
        if not isinstance(details, dict) or any(
            key in details and (type(details[key]) is not int or not 0 <= details[key] <= usage[limit])
            for key in keys
        ):
            raise ValueError("Invalid provider token details")
    output = raw.get("output")
    if not isinstance(output, list):
        raise ValueError("Provider output must be a list")
    calls = []
    for item in output:
        if not isinstance(item, dict) or item.get("type") not in {
            "reasoning",
            "message",
            "function_call",
        }:
            raise ValueError("Unsupported provider output item")
        if item["type"] == "message":
            if item.get("role", "assistant") != "assistant" or not isinstance(item.get("content"), list) or any(
                not isinstance(c, dict)
                or c.get("type") not in {"output_text", "refusal"}
                or not isinstance(c.get("text") if c.get("type") == "output_text" else c.get("refusal"), str)
                for c in item["content"]
            ):
                raise ValueError("Unsupported assistant message")
        if item["type"] == "reasoning" and item.get("encrypted_content") is not None and not isinstance(item["encrypted_content"], str):
            raise ValueError("Malformed encrypted reasoning item")
        if item["type"] != "function_call":
            continue
        if raw["status"] == "completed" and item.get("status") not in {None, "completed"}:
            raise ValueError("Completed response contains an unfinished function call")
        if any(
            not isinstance(item.get(k), str) or not item[k] for k in ("call_id", "name")
        ) or not isinstance(item.get("arguments"), str):
            raise ValueError("Malformed function call")
        arguments, _ = parse_arguments(item["arguments"])
        calls.append(
            {
                "call_id": item["call_id"],
                "name": item["name"],
                "arguments": arguments if isinstance(arguments, dict) else None,
                "raw_arguments": item["arguments"],
            }
        )
    return calls, usage


def append_turn(
    messages: list[dict], raw: dict, results: list[dict], feedback: bool,
    feedback_text: str | None = None,
) -> None:
    result_index = 0
    for item in deepcopy(raw["output"]):
        if item["type"] == "function_call":
            result = results[result_index]
            result_index += 1
            if item["call_id"] != result["transcript_call_id"]:
                item["call_id"] = result["transcript_call_id"]
                item.pop("id", None)
        messages.append(item)
    for result in results:
        messages.append({"type": "function_call_output", "call_id": result["transcript_call_id"],
                         "output": canonical_json(result["output"])})
    if feedback:
        messages.append(
            {
                "role": "user",
                "content": feedback_text,
            }
        )
