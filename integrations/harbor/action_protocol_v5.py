"""Deduplicate identical action envelopes; never choose between different intents."""
import json
import re

from . import action_protocol_v3 as v3, action_protocol_v4 as v4

PROTOCOL = "single_intent_actions_v5"
INSTRUCTIONS = v4.INSTRUCTIONS + """
One assistant response is one turn. After writing your single JSON object, end
the response immediately and wait for the controller's next message. Do not
repeat the object, append another actions object, or append final after actions.
If a response is rejected, no actions in it were executed. Correct the envelope
in your next response; this is not evidence that the public tools are unavailable.
For compatibility, exact semantic duplicates of one actions envelope are executed
only once. Different envelopes and actions mixed with final are never executed.
"""


def _commentary(text, *, side="prefix"):
    if not text:
        return
    if re.search(r"</?(?:[｜|]+DSML[｜|]+|DSML\b)", text):
        raise v3.ProtocolError("UNSUPPORTED_CALL_FORMAT",
            "Detected DSML call tags outside the JSON envelope. Use one text JSON actions envelope; "
            "no DSML calls were executed.", "$/" + side)
    if side == "suffix" and text.startswith(("]", "}")):
        raise v3.ProtocolError("EXTRA_TRAILING_CONTENT",
            "The JSON envelope is already complete. Remove the extra closing brackets AFTER it: "
            + json.dumps(text[:120], ensure_ascii=False) + ". No actions were executed.", "$/suffix")
    # A discussion of missing results is not itself a fabricated observation.
    if re.search(r"(?:^|\n)\s*(?:public\s+tool\s+results|tool\s+(?:results?|outputs?|responses?)|observation)\s*:", text, re.I):
        raise v3.ProtocolError("FABRICATED_OBSERVATION", "Omit simulated result/observation blocks; only the controller returns tool results.")
    if any(c in text for c in "{}"):
        raise v3.ProtocolError("EXTRA_JSON_CONTENT", "Unexpected JSON or braces outside the envelope; return one object and end the response.")
    if len(text) > v4.MAX_COMMENTARY_CHARS:
        raise v3.ProtocolError("AMBIGUOUS_WRAPPER", "Plain commentary on each side is limited to 1024 characters.")
    try:
        json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        pass
    except (ValueError, RecursionError) as error:
        if isinstance(error, ValueError) and not v3.is_integer_limit_error(error):
            raise
        raise v3.json_error(error) from error
    else:
        raise v3.ProtocolError("MULTIPLE_JSON", "An additional JSON value is not commentary. Return one envelope only.")
    try:
        v3.check_prefix(text)
    except v3.ProtocolError as error:
        if side == "prefix":
            raise
        raise v3.ProtocolError("TRAILING_CONTENT",
            "Invalid text AFTER the complete envelope. Remove trailing unmatched quotes, brackets "
            "or fences. No actions were executed.", "$/suffix") from error


def parse(text, *, defer_action_validation=False):
    if not isinstance(text, str) or "{" not in text:
        if isinstance(text, str) and "DSML" in text:
            _commentary(text.strip())
        return v3.parse(text, defer_action_validation=defer_action_validation)
    rest = text.strip()
    start = rest.find("{")
    prefix = rest[:start].strip()
    fenced = re.search(r"```(?:json)?\s*$", prefix, re.I)
    if fenced:
        prefix = prefix[:fenced.start()].strip()
    _commentary(prefix)
    decoder = json.JSONDecoder()
    spans = []
    cursor = start
    while True:
        try:
            value, end = decoder.raw_decode(rest, cursor)
        except (ValueError, RecursionError) as error:
            if not isinstance(error, (json.JSONDecodeError, RecursionError)) and not v3.is_integer_limit_error(error):
                raise
            if spans:
                raise v3.ProtocolError("MALFORMED_EXTRA_JSON",
                    f"Extra JSON starts at character {cursor + 1} but is malformed. No objects were executed; send only one complete envelope.") from None
            raise v3.json_error(error) from None
        # Reparse each complete value to enforce duplicate keys and finite values.
        strict = v3.parse_json(rest[cursor:end])
        spans.append((strict, cursor, end))
        cursor = end
        while cursor < len(rest) and rest[cursor].isspace():
            cursor += 1
        if cursor >= len(rest) or rest[cursor] != "{":
            break
    suffix = rest[cursor:].strip()
    if fenced:
        if not suffix.startswith("```"):
            raise v3.ProtocolError("AMBIGUOUS_WRAPPER", "Close the single JSON fence immediately after the envelope.")
        suffix = suffix[3:].strip()
    if len(spans) > 1:
        types = [v.get("type", "unknown") if isinstance(v, dict) else type(v).__name__ for v, _, _ in spans]
        canonical = [json.dumps(v, sort_keys=True, ensure_ascii=False, separators=(",", ":")) for v, _, _ in spans]
        identical_actions = all(t == "actions" for t in types) and len(set(canonical)) == 1
        if not identical_actions:
            code = "MIXED_ACTION_FINAL" if "actions" in types and "final" in types else "MULTIPLE_JSON"
            raise v3.ProtocolError(code,
                f"Found {len(spans)} envelopes ({', '.join(types[:8])}); the second begins at character {spans[1][1] + 1}. "
                "No objects were executed. Send one actions envelope OR one final envelope, then end the response. Do not simulate additional turns.")
    _commentary(suffix, side="suffix")
    first, first_start, first_end = spans[0]
    value, _ = v3.parse(rest[first_start:first_end], defer_action_validation=defer_action_validation)
    if value["type"] == "final" and (prefix or suffix or fenced):
        raise v3.ProtocolError("PREFIXED_FINAL", "Return final as standalone JSON without commentary or fences.")
    return value, len(rest) - (first_end - first_start)


def feedback(error):
    detail, message = v3.feedback(error)
    message += ("\nEnd the response immediately after the closing brace of that one envelope. "
                "Do not append any additional envelope or simulated tool results. "
                "Wait for the next controller message before deciding another turn.")
    return detail, message


def history_content(text, action=None):
    """Preserve the complete rejected turn, just as v4 does."""
    return v4.history_content(text, action)
