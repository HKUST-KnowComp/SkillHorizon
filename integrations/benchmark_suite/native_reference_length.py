"""Reference-only length meters; neither a runner nor provider usage accounting."""

from copy import deepcopy
from functools import lru_cache
from importlib.metadata import version
import json

import tiktoken


REVISION = "native_reference_length_v1"
BAND_REVISION = "native_reference_bands_v2"
BANDS = (64, 128, 256, 512)


def serialize(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False)


@lru_cache(maxsize=1)
def encoding():
    return tiktoken.get_encoding("o200k_base")


def tokens(value):
    return len(encoding().encode(serialize(value), disallowed_special=()))


def interval(band):
    if type(band) is not int or band not in BANDS:
        raise ValueError("Expected a supported reference band")
    target = band * 1024
    return target, target * 2


def band_label(length):
    if type(length) is not int or length < 0:
        raise ValueError("Expected a nonnegative integer reference length")
    return next((f"{band}k" for band in BANDS
                 if interval(band)[0] <= length < interval(band)[1]), None)


def contract():
    return {"revision": REVISION, "tokenizer": "o200k_base", "tiktoken_version": version("tiktoken"), "K": 1024,
            "serialization": "sorted compact UTF-8 JSON; each new message counted separately",
            "reference": "fixed public policy; uncompressed text-only native messages",
            "schemas": "structured tools once if present; prompt-embedded descriptions counted in messages",
            "business": "top-level public calls/results; gateway bookkeeping and elapsed times excluded",
            "excludes": ["repeated request prefixes", "hidden evaluator", "unsurfaced script internals",
                         "provider usage", "compaction summaries"],
            "not_claimed": ["shortest solution", "model behavior", "intrinsic context lower bound"]}


def text_message(value):
    if (not isinstance(value, dict) or set(value) != {"role", "content"}
            or value["role"] not in {"system", "developer", "user", "assistant"}
            or not isinstance(value["content"], str)):
        raise ValueError("Reference meter requires exact role/content text messages")
    return deepcopy(value)


class NativeReferenceMeter:
    """Consume actual fixture-bound requests and accepted responses, without retries.

    A shrinking or rewritten history is not silently reinterpreted as raw history.
    The runner must separately prove complete public execution and business success.
    """

    def __init__(self):
        self.messages = []
        self.events = []
        self.schema = None
        self.started = self.pending = self.closed = self.invalid = False
        self.total = self.schema_tokens = self.requests = 0

    def _reject(self, message):
        self.invalid = True
        raise ValueError(message)

    def _record(self, message, origin):
        count = tokens(message)
        self.total += count
        self.messages.append(deepcopy(message))
        self.events.append({"index": len(self.events), "origin": origin,
                            "message": deepcopy(message), "tokens": count})

    def request(self, messages, *, tools=None):
        if self.closed or self.invalid or self.pending:
            self._reject("Closed, invalid, or unresolved reference request")
        try:
            if not isinstance(messages, list) or not messages:
                raise ValueError("Nonempty native request messages required")
            if tools is not None and (not isinstance(tools, list) or any(not isinstance(t, dict) for t in tools)):
                raise ValueError("Structured tool schemas must be an array of objects")
            normalized = [text_message(message) for message in messages]
            schema = serialize(tools)
            if normalized[-1]["role"] != "user":
                raise ValueError("Native reference request must end with a user observation")
            if self.started:
                if schema != self.schema:
                    raise ValueError("Reference tool schema changed")
                if (len(normalized) <= len(self.messages)
                        or normalized[:len(self.messages)] != self.messages):
                    raise ValueError("History was retried, rewritten, truncated, or compacted")
                additions = normalized[len(self.messages):]
                if any(message["role"] != "user" for message in additions):
                    raise ValueError("Unexpected unrecorded assistant or instruction insertion")
            else:
                if any(message["role"] == "assistant" for message in normalized):
                    raise ValueError("Full-online reference cannot start with an assistant prefix")
                additions = normalized
                self.schema = schema
                self.schema_tokens = tokens(tools) if tools is not None else 0
            for message in additions:
                self._record(message, "initial" if not self.started else "native_observation")
            self.started = self.pending = True
            self.requests += 1
        except Exception as error:
            self._reject(str(error))

    def response(self, content):
        if self.closed or self.invalid or not self.pending or not isinstance(content, str):
            self._reject("An outstanding native request and accepted text response are required")
        try:
            self._record({"role": "assistant", "content": content}, "reference_response")
        except Exception as error:
            self._reject(str(error))
        self.pending = False

    def finish(self, *, native_complete, business_success):
        if self.closed:
            self._reject("Reference meter was already closed")
        self.closed = True
        valid = (self.started and not self.pending and not self.invalid
                 and native_complete is True and business_success is True)
        return {"contract": contract(), "status": "available" if valid else "invalid",
                "L_native_ref": self.total + self.schema_tokens if valid else None,
                "observed_message_tokens": self.total, "schema_tokens": self.schema_tokens,
                "native_complete": native_complete is True, "business_success": business_success is True,
                "pending": self.pending, "measurement_invalid": self.invalid,
                "requests": self.requests, "messages": len(self.messages),
                "scope": "reference measurement; native path must be independently verified"}


def business_length(events):
    """Measure committed gateway dispatch/result pairs, not lower-level duplicates."""
    total, calls, pending = 0, 0, None
    for event in events:
        if not isinstance(event, dict):
            raise ValueError("Invalid gateway event")
        sequence, phase = event.get("sequence"), event.get("phase")
        if type(sequence) is not int:
            raise ValueError("Integer gateway sequence required")
        if phase == "dispatch":
            if (pending is not None or sequence != calls + 1
                    or not isinstance(event.get("name"), str)
                    or not isinstance(event.get("arguments"), dict)):
                raise ValueError("Missing, duplicate, or overlapping gateway dispatch")
            pending = {"name": event["name"], "arguments": event["arguments"]}
        elif phase == "result":
            if pending is None or sequence != calls + 1 or "result" not in event:
                raise ValueError("Unmatched gateway result")
            total += tokens({"type": "call", **pending})
            total += tokens({"type": "result", "result": event["result"]})
            calls += 1
            pending = None
        else:
            raise ValueError("Rejected, uncertain, or unknown gateway event")
    if pending is not None or not calls:
        raise ValueError("No complete business reference calls")
    return {"L_business_ref": total, "top_level_calls": calls,
            "scope": "public gateway projection, not native visible history"}
