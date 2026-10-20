"""Public-policy loopback fixture; instance observations cross the terminal."""

import asyncio
import json
from pathlib import Path
from queue import Empty, Full, Queue
import re
import shlex
import threading
import time

from integrations.benchmark_suite.native_reference_length import NativeReferenceMeter
from .native_fixture import CONFIRMATION
from .reference_terminal_helper import BEGIN, CHUNK, DATA, END, READY


_HEADER = re.compile(r"^SH_PUBLIC_REFERENCE_BEGIN:(\d+):(\d+):(\d+):(\d+)$")
_PART = re.compile(r"^SH_PUBLIC_REFERENCE_DATA:(\d+):(.+)$")
_FOOTER = re.compile(r"^SH_PUBLIC_REFERENCE_END:(\d+):(\d+)$")
_HELPER = "/tmp/skillhorizon_reference_helper.py"


class PublicPolicyBridge:
    def __init__(self, factory, *, timeout=60):
        self.factory, self.timeout = factory, timeout
        self.actions, self.results = Queue(maxsize=1), Queue(maxsize=1)
        self.stopped = threading.Event()
        self.thread = None

    async def invoke(self, name, arguments):
        if self.stopped.is_set():
            raise RuntimeError("Reference policy stopped")
        self.actions.put(("call", {"name": name, "arguments": arguments}), timeout=self.timeout)
        result = self.results.get(timeout=self.timeout)
        if self.stopped.is_set():
            raise RuntimeError("Reference policy stopped")
        return result

    def _run(self):
        try:
            event = ("complete", asyncio.run(self.factory(self.invoke).run()))
        except Exception as error:
            event = ("error", {"type": type(error).__name__})
        if not self.stopped.is_set():
            try:
                self.actions.put(event, timeout=self.timeout)
            except Full:
                self.stopped.set()

    def next(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()
        try:
            return self.actions.get(timeout=self.timeout)
        except Empty:
            raise RuntimeError("Public policy produced no next action") from None

    def observed(self, result):
        self.results.put(result, timeout=self.timeout)

    def close(self):
        self.stopped.set()
        try:
            self.results.put_nowait(None)
        except Full:
            pass
        if self.thread is not None:
            self.thread.join(timeout=5)
            if self.thread.is_alive():
                raise RuntimeError("Reference policy thread did not stop")


def setup_command():
    source = Path(__file__).with_name("reference_terminal_helper.py").read_text(encoding="utf-8")
    if "\nSH_PUBLIC_REFERENCE_HELPER\n" in source:
        raise ValueError("Unsafe reference helper delimiter")
    return ("cat > " + _HELPER + " <<'SH_PUBLIC_REFERENCE_HELPER'\n" + source
            + "\nSH_PUBLIC_REFERENCE_HELPER\npython " + _HELPER + " --ready\n")


def tool_command(ordinal, action):
    """Page a public result from an agent-owned file, never re-call the tool."""
    arguments = shlex.quote(json.dumps(action, ensure_ascii=True, separators=(",", ":"), allow_nan=False))
    return f"python {_HELPER} call {arguments} {ordinal}\n"


def page_command(ordinal, offset):
    return f"python {_HELPER} page {ordinal} {offset}\n"


class FrameCollector:
    """Merge short physical terminal lines across incremental or repeated captures."""

    def __init__(self, ordinal, offset):
        self.ordinal, self.offset = ordinal, offset
        self.total = self.count = None
        self.parts = {}
        self.ended = self.active = False

    def feed(self, observation):
        for line in observation.splitlines():
            line = line.rstrip("\r")
            if line.startswith(BEGIN):
                match = _HEADER.fullmatch(line)
                if match is None:
                    raise ValueError("Malformed public terminal header")
                call, offset, total, count = map(int, match.groups())
                self.active = (call, offset) == (self.ordinal, self.offset)
                if not self.active:
                    continue
                if total < 1 or not 0 <= offset < total or not 1 <= count <= 64:
                    raise ValueError("Invalid public terminal page")
                if self.total is not None and (total, count) != (self.total, self.count):
                    raise ValueError("Public terminal page changed")
                self.total, self.count = total, count
            elif line.startswith(DATA):
                if not self.active:
                    continue
                match = _PART.fullmatch(line)
                if match is None or self.total is None:
                    raise ValueError("Malformed public terminal data")
                index = int(match[1])
                try:
                    value = json.loads(match[2])
                except ValueError as error:
                    raise ValueError("Malformed public terminal segment") from error
                if not isinstance(value, str) or not value or not value.isascii():
                    raise ValueError("Invalid public terminal segment")
                if index >= self.count or (index in self.parts and self.parts[index] != value):
                    raise ValueError("Conflicting public terminal data")
                self.parts[index] = value
            elif line.startswith(END):
                if not self.active:
                    continue
                match = _FOOTER.fullmatch(line)
                if match is None or (int(match[1]), int(match[2])) != (self.ordinal, self.offset):
                    raise ValueError("Wrong public terminal footer")
                self.ended = True
        if not self.ended or self.total is None or len(self.parts) != self.count:
            return None
        try:
            chunk = "".join(self.parts[i] for i in range(self.count)).encode("ascii")
        except (KeyError, UnicodeError) as error:
            raise ValueError("Invalid public terminal encoding") from error
        if len(chunk) != min(CHUNK, self.total - self.offset):
            raise ValueError("Invalid public terminal byte count")
        return {"call": self.ordinal, "offset": self.offset, "total": self.total, "chunk": chunk}


def observed_frame(text, ordinal, offset):
    frame = FrameCollector(ordinal, offset).feed(text)
    if frame is None:
        raise ValueError("Incomplete public terminal frame")
    return frame


class ReferenceActor:
    def __init__(self, factory, *, timeout=300, on_events=None, wait_seconds=130, poll_delay=1):
        self.bridge = PublicPolicyBridge(factory, timeout=timeout)
        self.meter = NativeReferenceMeter()
        self.on_events = on_events or (lambda events: None)
        self.ordinal = self.offset = 0
        self.total = None
        self.chunks = []
        self.collector = None
        self.pending_since = None
        self.wait_seconds, self.poll_delay = wait_seconds, poll_delay
        self.setup_sent = self.setup_done = False
        self.pending = self.complete_sent = self.native_complete = False
        self.policy_result = None
        self.failure = None
        self.lock = threading.Lock()
        self.requests = 0

    def respond(self, payload):
        with self.lock:
            try:
                return self._respond(payload)
            except Exception as error:
                self.failure = type(error).__name__
                self.meter.invalid = True
                raise

    def _respond(self, payload):
        if self.failure or self.native_complete:
            raise ValueError("Reference fixture already terminated")
        self.requests += 1
        before = len(self.meter.events)
        self.meter.request(payload["messages"], tools=payload.get("tools"))
        observation = payload["messages"][-1]["content"]
        command = None
        waiting = False
        if self.complete_sent:
            if not observation.endswith(CONFIRMATION):
                raise ValueError("Missing native completion confirmation")
            self.native_complete = True
        else:
            if not self.setup_done:
                if not self.setup_sent:
                    command = setup_command()
                    self.setup_sent = True
                    self.pending_since = time.monotonic()
                elif READY in observation.splitlines():
                    self.setup_done = True
                else:
                    if time.monotonic() - self.pending_since > self.wait_seconds:
                        raise TimeoutError("Public terminal helper did not initialize")
                    waiting = True
                    if self.poll_delay:
                        time.sleep(self.poll_delay)
            if self.setup_done and self.pending:
                frame = self.collector.feed(observation)
                if frame is None:
                    if time.monotonic() - self.pending_since > self.wait_seconds:
                        raise TimeoutError("Public terminal frame did not complete")
                    waiting = True
                    if self.poll_delay:
                        time.sleep(self.poll_delay)
                else:
                    if self.total is not None and self.total != frame["total"]:
                        raise ValueError("Public terminal file changed while paging")
                    self.total = frame["total"]
                    self.chunks.append(frame["chunk"])
                    self.offset += len(frame["chunk"])
                    if self.offset < self.total:
                        command = page_command(self.ordinal, self.offset)
                        self.collector = FrameCollector(self.ordinal, self.offset)
                        self.pending_since = time.monotonic()
                    else:
                        record = json.loads(b"".join(self.chunks).decode("ascii"))
                        if (not isinstance(record, dict) or set(record) != {"call", "payload"}
                                or type(record["call"]) is not int or record["call"] != self.ordinal
                                or not isinstance(record["payload"], dict)):
                            raise ValueError("Public result identity mismatch")
                        self.bridge.observed(record["payload"])
                        self.pending = False
            if self.setup_done and command is None and not waiting:
                kind, value = self.bridge.next()
                if kind == "error":
                    raise ValueError("Public policy failed: " + value["type"])
                if kind == "complete":
                    self.policy_result = value
                    self.complete_sent = True
                else:
                    self.ordinal += 1
                    self.offset, self.total, self.chunks = 0, None, []
                    self.pending = True
                    self.collector = FrameCollector(self.ordinal, 0)
                    self.pending_since = time.monotonic()
                    command = tool_command(self.ordinal, value)
        response = {"analysis": "Deterministic public reference control, not model reasoning.",
                    "plan": "Await the public result or execute the next public operation.",
                    "commands": ([{"keystrokes": command, "duration": 1}] if command else []),
                    "task_complete": self.complete_sent}
        self.meter.response(json.dumps(response))
        self.on_events(self.meter.events[before:])
        return response

    def close(self):
        self.bridge.close()
