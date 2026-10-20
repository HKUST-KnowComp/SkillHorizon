"""Accept a bounded closing-delimiter suffix after one intact action envelope."""
import json
import re

from . import action_protocol_v3 as v3, action_protocol_v4 as v4

PROTOCOL = "complete_actions_bounded_suffix_v6"
FEEDBACK_VERSION = "bounded_suffix_stack_feedback_v2"
MAX_SUFFIX_CHARS = 64
MAX_EXTRA_CLOSERS = 8
INSTRUCTIONS = v4.INSTRUCTIONS + """
Transport framing compatibility: after a complete, valid actions object only,
up to eight stray closing delimiters and one terminal <|eos|> marker may be
ignored within a 64-character suffix. The action body is never repaired.
This does not apply to final, truncated replies, multiple JSON objects, or
unmatched delimiters inside an object. Continue to output exactly one JSON
object and stop immediately after it.
"""


def closing_suffix(text):
    """The whole suffix must be non-operative; never search for another body."""
    return (0 < len(text) <= MAX_SUFFIX_CHARS
            and 1 <= sum(c in "}]" for c in text) <= MAX_EXTRA_CLOSERS
            and re.fullmatch(r"[\s}\]]+(?:<\|eos\|>\s*)?", text) is not None)


def parse(text, *, defer_action_validation=False):
    try:
        return v4.parse(text, defer_action_validation=defer_action_validation)
    except v3.ProtocolError as error:
        # Only this known wrapper failure is eligible. Other parsing and
        # schema errors retain their original rejection and feedback.
        if error.detail["code"] != "EXTRA_TRAILING_CONTENT" or not isinstance(text, str):
            raise
        rest = text.strip()
        if not rest.startswith("{"):
            raise
        _, end = json.JSONDecoder().raw_decode(rest)
        suffix = rest[end:]
        if not closing_suffix(suffix):
            raise
        # Validate the original complete bytes, including every action. A
        # deferred caller must not use suffix recovery to admit a bad batch.
        value, _ = v3.parse(rest[:end])
        if value["type"] != "actions":
            raise error
        return value, len(suffix)


def feedback(error):
    return v4.feedback(error)


def history_content(text, action=None):
    return v4.history_content(text, action)
