"""Accept one closed, non-operative think wrapper before a complete final."""

import re

from . import action_protocol_v3 as v3, action_protocol_v4 as v4


PROTOCOL = "closed_think_final_v7"
INSTRUCTIONS = v4.INSTRUCTIONS + (
    "\nA single closed <think>...</think> block before a complete final JSON "
    "is treated as non-operative text. The final JSON must be the only content "
    "after that block. Prefer standalone JSON without the block.\n"
)
WRAPPER = re.compile(r"\A\s*<think>[\s\S]{0,900}?</think>\s*")


def parse(text, *, defer_action_validation=False):
    try:
        return v4.parse(text, defer_action_validation=defer_action_validation)
    except v3.ProtocolError as original:
        if original.detail["code"] != "PREFIXED_FINAL" or not isinstance(text, str):
            raise
        match = WRAPPER.match(text)
        if match is None:
            raise
        body = text[match.end():]
        value, wrapper = v4.parse(body, defer_action_validation=defer_action_validation)
        if value["type"] != "final" or wrapper:
            raise original
        return value, match.end()


def feedback(error):
    return v4.feedback(error)


def history_content(text, action=None):
    return v4.history_content(text, action)
