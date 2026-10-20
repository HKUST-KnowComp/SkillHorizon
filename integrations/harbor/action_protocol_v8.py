"""Accept a closed think wrapper and a bounded non-operative actions suffix."""

import re

from . import action_protocol_v3 as v3, action_protocol_v6 as v6, action_protocol_v7 as v7


PROTOCOL = "closed_think_actions_suffix_v8"
INSTRUCTIONS = v7.INSTRUCTIONS + (
    '\nFor actions only, one closed <think> block before a complete, '
    'schema-valid JSON object and at most eight stray closing delimiters '
    'within a 64-character suffix may be ignored. Neither the action body '
    'nor final is repaired.\n'
)
WRAPPER = re.compile(r"\A\s*<think>[\s\S]{0,900}?</think>\s*")


def parse(text, *, defer_action_validation=False):
    try:
        return v7.parse(text, defer_action_validation=defer_action_validation)
    except v3.ProtocolError as original:
        if original.detail['code'] != 'EXTRA_TRAILING_CONTENT' or not isinstance(text, str):
            raise
        match = WRAPPER.match(text)
        if match is None:
            raise
        value, suffix_chars = v6.parse(text[match.end():],
                                       defer_action_validation=defer_action_validation)
        if value['type'] != 'actions' or suffix_chars == 0:
            raise original
        return value, match.end() + suffix_chars


def feedback(error):
    return v6.feedback(error)


def history_content(text, action=None):
    return v7.history_content(text, action)
