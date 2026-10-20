"""Bounded, stdlib-only Stage E duplex framing, suitable for copying to a guest.

JSON limits use root depth zero and count every value AND every dictionary key.
Only exact builtins are JSON values; integers are signed 64-bit. Encoding matches
canonical_json: sorted keys, ensure_ascii=True, compact separators. max_bytes is
JSON-only for bounded_json_bytes, but includes the four-byte header everywhere
else. Decoding checks both actual wire bytes and canonical JSON size so journal
re-encoding cannot bypass the same capacity contract.

feed() returns complete envelopes, retaining a partial next frame. Errors poison
the decoder; a failed feed returns no partial batch. initial does not close it;
final/terminate reject any subsequent nonempty input. finish() marks transport
EOF and rejects truncation, but does not require an application terminal frame.
Pairing, direction, lifecycle ordering and payload schemas belong to the bridge.
Do not mutate input objects concurrently with encoding.
"""

import json
import math
import struct


HEADER_BYTES = 4
MIN_JSON_INTEGER = -(1 << 63)
MAX_JSON_INTEGER = (1 << 63) - 1
FRAME_TYPES = frozenset({"initial", "child_request", "child_response", "final", "terminate"})
_FIELDS = frozenset({"type", "seq", "payload"})


class ProtocolError(ValueError):
    """Invalid data or an exceeded bound; code never contains input payloads."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _limit(value, name, minimum=0):
    if type(value) is not int or value < minimum:
        raise ProtocolError("INVALID_" + name.upper())


def _limits(max_bytes, max_depth, max_nodes):
    _limit(max_bytes, "max_bytes")
    _limit(max_depth, "max_depth")
    _limit(max_nodes, "max_nodes", 1)


def _string_size(value, remaining):
    # Even one huge scalar must be rejected before json.dumps allocates its
    # escaped representation. Scan at most the available output capacity.
    size = len(value) + 2
    if size > remaining:
        raise ProtocolError("BYTE_LIMIT")
    for char in value:
        point = ord(char)
        if 0xD800 <= point <= 0xDFFF:
            raise ProtocolError("INVALID_SURROGATE")
        if char in '"\\\b\f\n\r\t':
            size += 1
        elif point < 32 or 127 <= point <= 0xFFFF:
            size += 5
        elif point > 0xFFFF:
            size += 11
        if size > remaining:
            raise ProtocolError("BYTE_LIMIT")
    return size


def _children(value):
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise ProtocolError("INVALID_KEY")
            yield key
            yield item
    else:
        yield from value


def _measure(value, max_bytes, max_depth, max_nodes):
    size = nodes = 0
    active = set()
    stack = [(iter((value,)), 0, None)]
    while stack:
        iterator, depth, identity = stack[-1]
        try:
            item = next(iterator)
        except StopIteration:
            stack.pop()
            if identity is not None:
                active.remove(identity)
            continue
        nodes += 1
        if nodes > max_nodes:
            raise ProtocolError("NODE_LIMIT")
        if depth > max_depth:
            raise ProtocolError("DEPTH_LIMIT")
        kind = type(item)
        if kind in (dict, list):
            count = len(item) * (2 if kind is dict else 1)
            if nodes + count > max_nodes:
                raise ProtocolError("NODE_LIMIT")
            if id(item) in active:
                raise ProtocolError("CYCLIC_JSON")
            size += (count + 1) if count else 2
            active.add(id(item))
            stack.append((iter(_children(item)), depth + 1, id(item)))
        elif kind is str:
            size += _string_size(item, max_bytes - size)
        elif kind is int:
            if not MIN_JSON_INTEGER <= item <= MAX_JSON_INTEGER:
                raise ProtocolError("INTEGER_LIMIT")
            size += len(str(item))
        elif kind is float:
            if not math.isfinite(item):
                raise ProtocolError("NONFINITE_JSON")
            size += len(json.dumps(item, allow_nan=False))
        elif item is None:
            size += 4
        elif kind is bool:
            size += 4 if item else 5
        else:
            raise ProtocolError("NONPLAIN_JSON")
        if size > max_bytes:
            raise ProtocolError("BYTE_LIMIT")
    return size


def _pieces(value):
    """Iterative serialization after the entire structure passes bounded checks."""
    def children(item):
        if type(item) is dict:
            yield "{", True
            for index, key in enumerate(sorted(item)):
                if index:
                    yield ",", True
                yield key, False
                yield ":", True
                yield item[key], False
            yield "}", True
        else:
            yield "[", True
            for index, child in enumerate(item):
                if index:
                    yield ",", True
                yield child, False
            yield "]", True

    stack = [iter(((value, False),))]
    while stack:
        try:
            item, literal = next(stack[-1])
        except StopIteration:
            stack.pop()
            continue
        if literal:
            yield item.encode("ascii")
        elif type(item) in (dict, list):
            stack.append(iter(children(item)))
        else:
            yield json.dumps(item, ensure_ascii=True, allow_nan=False,
                             separators=(",", ":")).encode("ascii")


def bounded_json_bytes(value, *, max_bytes, max_depth, max_nodes):
    """Return canonical JSON bytes, checking structure and size before encoding."""
    _limits(max_bytes, max_depth, max_nodes)
    expected = _measure(value, max_bytes, max_depth, max_nodes)
    output = bytearray()
    for piece in _pieces(value):
        if len(output) + len(piece) > max_bytes:
            raise ProtocolError("BYTE_LIMIT")
        output.extend(piece)
    if len(output) != expected:
        raise ProtocolError("JSON_CHANGED")
    return bytes(output)


def _envelope(value):
    if type(value) is not dict or len(value) != 3:
        raise ProtocolError("INVALID_ENVELOPE")
    if any(type(key) is not str or key not in _FIELDS for key in value):
        raise ProtocolError("INVALID_ENVELOPE")
    if type(value["type"]) is not str or value["type"] not in FRAME_TYPES:
        raise ProtocolError("INVALID_FRAME_TYPE")
    if type(value["seq"]) is not int or not 0 <= value["seq"] <= MAX_JSON_INTEGER:
        raise ProtocolError("INVALID_SEQ")


def encode_frame(value, *, max_bytes, max_depth, max_nodes):
    """Return uint32 big-endian payload length followed by canonical JSON."""
    _limits(max_bytes, max_depth, max_nodes)
    _envelope(value)
    if max_bytes < HEADER_BYTES:
        raise ProtocolError("BYTE_LIMIT")
    payload = bounded_json_bytes(value, max_bytes=min(max_bytes - HEADER_BYTES, 0xFFFFFFFF),
                                 max_depth=max_depth, max_nodes=max_nodes)
    return struct.pack(">I", len(payload)) + payload


def _integer(token):
    if len(token.lstrip("-")) > 19:
        raise ProtocolError("INTEGER_LIMIT")
    value = int(token)
    if not MIN_JSON_INTEGER <= value <= MAX_JSON_INTEGER:
        raise ProtocolError("INTEGER_LIMIT")
    return value


def _constant(token):
    raise ProtocolError("NONFINITE_JSON")


def _parse(payload, max_depth, max_nodes):
    """Use stdlib for scalar grammar, explicit stacks for bounded containers."""
    source = payload.decode("utf-8", errors="strict")
    scalar = json.JSONDecoder(parse_int=_integer, parse_constant=_constant)
    pos = nodes = 0
    stack = []

    def whitespace():
        nonlocal pos
        while pos < len(source) and source[pos] in " \t\n\r":
            pos += 1

    def read(depth, key=False):
        nonlocal pos, nodes
        nodes += 1
        if nodes > max_nodes:
            raise ProtocolError("NODE_LIMIT")
        if depth > max_depth:
            raise ProtocolError("DEPTH_LIMIT")
        whitespace()
        if pos == len(source):
            raise ProtocolError("INVALID_JSON")
        char = source[pos]
        if key and char != '"':
            raise ProtocolError("INVALID_KEY")
        if char in "[{":
            pos += 1
            value = [] if char == "[" else {}
            stack.append([value, depth, "first", None])
            return value
        value, pos = scalar.raw_decode(source, pos)
        if type(value) is float and not math.isfinite(value):
            raise ProtocolError("NONFINITE_JSON")
        if type(value) is str:
            for char in value:
                if 0xD800 <= ord(char) <= 0xDFFF:
                    raise ProtocolError("INVALID_SURROGATE")
        return value

    root = read(0)
    while stack:
        frame = stack[-1]
        container, depth, state, key = frame
        whitespace()
        if pos == len(source):
            raise ProtocolError("INVALID_JSON")
        char = source[pos]
        closing = "]" if type(container) is list else "}"
        if state in ("first", "after") and char == closing:
            pos += 1
            stack.pop()
        elif state == "after":
            if char != ",":
                raise ProtocolError("INVALID_JSON")
            pos += 1
            frame[2] = "next"
        elif type(container) is list:
            frame[2] = "after"
            container.append(read(depth + 1))
        elif state in ("first", "next"):
            key = read(depth + 1, key=True)
            if key in container:
                raise ProtocolError("DUPLICATE_KEY")
            frame[3] = key
            frame[2] = "colon"
        elif state == "colon":
            if char != ":":
                raise ProtocolError("INVALID_JSON")
            pos += 1
            frame[2] = "value"
        else:
            frame[2] = "after"
            container[key] = read(depth + 1)
    whitespace()
    if pos != len(source):
        raise ProtocolError("INVALID_JSON")
    return root


class IncrementalFrameDecoder:
    """One receive direction; total_bytes includes all consumed frame headers.

    The bridge must share a separate budget across both directions. A declared
    length exceeding the remaining stream capacity fails as soon as its header
    arrives, before allocating or waiting for the payload.
    """

    def __init__(self, max_bytes, max_depth, max_nodes, max_total_bytes):
        _limits(max_bytes, max_depth, max_nodes)
        _limit(max_total_bytes, "max_total_bytes")
        self.max_bytes = max_bytes
        self.max_depth = max_depth
        self.max_nodes = max_nodes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0
        self._buffer = bytearray()
        self._length = None
        self._failed = self._finished = self._terminal = False

    def feed(self, chunk):
        """Consume bytes/bytearray/contiguous memoryview; return a list of frames."""
        if self._failed or self._finished:
            raise ProtocolError("DECODER_CLOSED")
        try:
            if type(chunk) not in (bytes, bytearray, memoryview):
                raise ProtocolError("INVALID_CHUNK")
            data = memoryview(chunk).cast("B")
            if self.total_bytes + len(data) > self.max_total_bytes:
                raise ProtocolError("TOTAL_BYTE_LIMIT")
            frames = []
            pos = 0
            while pos < len(data):
                if self._terminal:
                    raise ProtocolError("TRAILING_DATA")
                target = HEADER_BYTES if self._length is None else self._length
                count = min(target - len(self._buffer), len(data) - pos)
                self._buffer.extend(data[pos:pos + count])
                self.total_bytes += count
                pos += count
                if len(self._buffer) != target:
                    continue
                if self._length is None:
                    self._length = struct.unpack(">I", self._buffer)[0]
                    self._buffer.clear()
                    if not self._length or self._length + HEADER_BYTES > self.max_bytes:
                        raise ProtocolError("INVALID_FRAME_LENGTH")
                    if self.total_bytes + self._length > self.max_total_bytes:
                        raise ProtocolError("TOTAL_BYTE_LIMIT")
                else:
                    value = _parse(self._buffer, self.max_depth, self.max_nodes)
                    _envelope(value)
                    _measure(value, self.max_bytes - HEADER_BYTES,
                             self.max_depth, self.max_nodes)
                    self._buffer.clear()
                    self._length = None
                    frames.append(value)
                    self._terminal = value["type"] in ("final", "terminate")
            return frames
        except (ValueError, TypeError, UnicodeError, OverflowError, RecursionError) as error:
            self._failed = True
            self._buffer.clear()
            if isinstance(error, ProtocolError):
                raise
            raise ProtocolError("INVALID_JSON_OR_CHUNK") from None

    def finish(self):
        """Declare transport EOF; idempotent on success, fail closed on partial data."""
        if self._failed:
            raise ProtocolError("DECODER_CLOSED")
        if self._buffer or self._length is not None:
            self._failed = True
            self._buffer.clear()
            raise ProtocolError("TRUNCATED_FRAME")
        self._finished = True
