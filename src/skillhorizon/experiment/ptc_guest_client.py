"""Minimal guest-only client, installed as skillhorizon_tools by the bootstrap.

The descriptor reaches only the container supervisor. This client is untrusted
from the host's perspective: the host independently enforces all permissions,
sequence checks and budgets even if guest code changes or bypasses this module.
"""

import math
import socket
import threading
import time

try:
    from .ptc_protocol import IncrementalFrameDecoder, encode_frame, ProtocolError
except ImportError:
    from ptc_protocol import IncrementalFrameDecoder, encode_frame, ProtocolError


class BridgeClosedError(RuntimeError):
    pass


class ProgramLimitError(BridgeClosedError):
    code = "PTC_PROGRAM_LIMIT"
    scope = "current_program"

    def __init__(self):
        super().__init__(
            "PTC_PROGRAM_LIMIT: scope=current_program. The controller stopped this program's bridge "
            "at a declared limit. Already completed business operations have not been rolled back. "
            "This does not by itself close the episode's business tools; further calls remain "
            "subject to existing permissions and budgets. Inspect current state before continuing "
            "to avoid repeating completed operations."
        )


class _Client:
    def __init__(self, fd, config, deadline):
        if type(fd) is not int or fd < 3:
            raise ValueError("A dedicated bridge descriptor is required")
        if type(deadline) not in (float, int) or not math.isfinite(deadline) or deadline <= time.monotonic():
            raise ValueError("A future execution deadline is required")
        self.config = dict(config)
        self.deadline = deadline
        self.decoder = IncrementalFrameDecoder(config["max_response_bytes"], config["max_json_depth"],
                                               config["max_json_nodes"], config["max_bridge_bytes"])
        self.channel = socket.socket(fileno=fd)
        self.lock = threading.Lock()
        self.seq = 0
        self.sent_bytes = 0
        self.closed = False

    def close(self):
        self.closed = True
        self.channel.close()

    def _remaining(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise BridgeClosedError("PTC_EXECUTION_DEADLINE")
        self.channel.settimeout(remaining)

    def call(self, name, arguments):
        # Ordinary clients are serial. The supervisor remains responsible for
        # rejecting bypassed/reloaded/forked-client frames, not this lock.
        with self.lock:
            if self.closed:
                raise BridgeClosedError("PTC_BRIDGE_CLOSED")
            self.seq += 1
            try:
                frame = encode_frame({"type": "child_request", "seq": self.seq,
                                      "payload": {"name": name, "arguments": arguments}},
                                     max_bytes=self.config["max_request_bytes"],
                                     max_depth=self.config["max_json_depth"], max_nodes=self.config["max_json_nodes"])
                if self.sent_bytes + len(frame) + self.decoder.total_bytes > self.config["max_bridge_bytes"]:
                    raise BridgeClosedError("PTC_BRIDGE_CAPACITY")
                self._remaining()
                self.channel.sendall(frame)
                self.sent_bytes += len(frame)
                while True:
                    self._remaining()
                    block = self.channel.recv(16384)
                    if not block:
                        self.decoder.finish()
                        raise BridgeClosedError("PTC_BRIDGE_DISCONNECTED")
                    frames = self.decoder.feed(block)
                    if self.sent_bytes + self.decoder.total_bytes > self.config["max_bridge_bytes"]:
                        raise BridgeClosedError("PTC_BRIDGE_CAPACITY")
                    if not frames:
                        continue
                    if len(frames) != 1:
                        raise BridgeClosedError("PTC_RESPONSE_ORDER_INVALID")
                    response = frames[0]
                    if response["seq"] != self.seq:
                        raise BridgeClosedError("PTC_RESPONSE_ORDER_INVALID")
                    if response["type"] == "terminate":
                        if response["payload"] == {"code": "PTC_PROGRAM_LIMIT"}:
                            raise ProgramLimitError()
                        raise BridgeClosedError("PTC_BRIDGE_TERMINATED")
                    if response["type"] != "child_response":
                        raise BridgeClosedError("PTC_RESPONSE_ORDER_INVALID")
                    if type(response["payload"]) is not dict:
                        raise BridgeClosedError("PTC_RESPONSE_INVALID")
                    return response["payload"]
            except ProgramLimitError:
                self.close()
                raise
            except (OSError, ValueError, BridgeClosedError):
                self.close()
                raise BridgeClosedError("PTC_BRIDGE_CLOSED") from None


_client = None


def _initialize(fd, config, deadline):
    global _client
    if _client is not None:
        raise ValueError("Bridge client is already initialized")
    _client = _Client(fd, config, deadline)


def call(name, arguments):
    """Call one exported business tool and return its original JSON observation."""
    if _client is None:
        raise BridgeClosedError("PTC_BRIDGE_NOT_ENABLED")
    return _client.call(name, arguments)
