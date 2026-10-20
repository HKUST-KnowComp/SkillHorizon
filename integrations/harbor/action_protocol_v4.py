"""Accept harmless text wrappers without repairing or guessing an action body."""
import json
import re

from . import action_protocol_v3 as v3
from .action_protocol_v3 import ProtocolError, schema_detail

PROTOCOL = "bounded_wrappers_actions_v4"
FEEDBACK_VERSION = "located_rejection_raw_history_v3"
MAX_COMMENTARY_CHARS = 1024
INSTRUCTIONS = v3.INSTRUCTIONS.replace(
    "Up to 1024 characters of leading commentary may precede one complete actions\n"
    "object, including balanced inline backticks, quotes and brackets. Commentary is\n"
    "non-operative. No code fences, multiple JSON values, trailing prose, or prefixed\n"
    "final envelopes.",
    "One complete actions object may have up to 1024 characters of plain commentary\n"
    "on either side and an optional single Markdown JSON code fence. Commentary is\n"
    "non-operative and is discarded. Never include multiple JSON values or fabricate\n"
    "Public tool results/observations. Final envelopes must remain standalone JSON.")
INSTRUCTIONS += "\nEnd the response immediately after the complete envelope. Do not append extra closing brackets.\n"


def check_commentary(text, *, side="prefix"):
    if not text:
        return
    if re.search(r"</?(?:[｜|]+DSML[｜|]+|DSML\b)", text):
        raise ProtocolError("UNSUPPORTED_CALL_FORMAT",
            "Detected DSML call tags outside the JSON envelope. This interface accepts text JSON actions, "
            "not DSML markup. Resubmit the intended calls using name and arguments inside one actions array. "
            "No DSML calls were executed.", "$/" + side)
    if re.search(r"public\s+tool\s+results|(?:^|\n)\s*(?:tool\s+(?:results?|outputs?|responses?)|observation)\s*:", text, re.I):
        raise ProtocolError("FABRICATED_OBSERVATION", "Only the controller supplies tool results; omit simulated observations.")
    if len(text) > MAX_COMMENTARY_CHARS:
        raise ProtocolError("AMBIGUOUS_WRAPPER", "Commentary on each side must be at most 1024 characters.")
    if side == "suffix" and text.startswith(("]", "}")):
        raise ProtocolError("EXTRA_TRAILING_CONTENT",
            "The JSON envelope is already complete. Remove the extra closing brackets AFTER it: "
            + json.dumps(text[:120], ensure_ascii=False) + ". No actions were executed.", "$/suffix")
    if any(c in text for c in "{}"):
        raise ProtocolError("MULTIPLE_JSON", "Return exactly one JSON envelope, without additional objects.")
    try:
        json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        pass
    except (ValueError, RecursionError) as error:
        if isinstance(error, ValueError) and not v3.is_integer_limit_error(error):
            raise
        raise v3.json_error(error) from error
    else:
        raise ProtocolError("MULTIPLE_JSON", "Commentary must not contain an additional JSON value.")
    try:
        v3.check_prefix(text)
    except ProtocolError as error:
        if side == "prefix":
            raise
        raise ProtocolError("TRAILING_CONTENT",
            "Invalid text AFTER the complete envelope. Remove the trailing unmatched quotes, brackets or fences. "
            "No actions were executed.", "$/suffix") from error


def parse(text, *, defer_action_validation=False):
    if not isinstance(text, str):
        return v3.parse(text, defer_action_validation=defer_action_validation)
    rest = text.strip()
    start = rest.find("{")
    if start < 0:
        if "DSML" in rest:
            check_commentary(rest)
        return v3.parse(text, defer_action_validation=defer_action_validation)
    prefix = rest[:start].strip()
    fenced = re.search(r"```(?:json)?\s*$", prefix, re.I)
    if fenced:
        prefix = prefix[:fenced.start()].strip()
    check_commentary(prefix)
    body = rest[start:]
    try:
        _, end = json.JSONDecoder().raw_decode(body)
    except (ValueError, RecursionError) as error:
        if not isinstance(error, (json.JSONDecodeError, RecursionError)) and not v3.is_integer_limit_error(error):
            raise
        raise v3.json_error(error) from None
    suffix = body[end:].strip()
    if fenced:
        if not suffix.startswith("```"):
            raise ProtocolError("AMBIGUOUS_WRAPPER", "Close the single JSON code fence after the envelope.")
        suffix = suffix[3:].strip()
    if suffix and any(c in suffix for c in "{}") and not suffix.startswith("{"):
        raise ProtocolError("EXTRA_TRAILING_CONTENT",
            f"One complete JSON object ends at character {start + end}. "
            f"Unexpected suffix: {json.dumps(suffix[:180], ensure_ascii=False)}. "
            "Remove the suffix AFTER the envelope, including extra closing brackets; do not resend it. "
            "No actions were executed.", "$/suffix")
    check_commentary(suffix, side="suffix")
    # v3 enforces duplicate-key, finite-number, depth and exact action schemas.
    value, _ = v3.parse(body[:end], defer_action_validation=defer_action_validation)
    wrapper_chars = len(rest) - end
    if value["type"] == "final" and wrapper_chars:
        raise ProtocolError("PREFIXED_FINAL", "Return final as standalone JSON without commentary or fences.")
    return value, wrapper_chars


def feedback(error):
    detail, message = v3.feedback(error)
    message += "\nEnd immediately after the single envelope."
    if detail["code"] not in {"PREFIXED_FINAL", "FINAL_SCHEMA"}:
        message += (" For syntax correction, prefer a small actions envelope "
                    "and wait for real tool feedback before sending more.")
    return detail, message


def history_content(text, action=None):
    """Keep rejected model text in its actual role for correction, never execution."""
    if action is None:
        # Preserve the model's actual turn. Rejection metadata belongs to the
        # following controller feedback, never an invented assistant quotation.
        return text
    return json.dumps(action, ensure_ascii=False, separators=(",", ":"))
