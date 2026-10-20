"""Controller-only one-use grants and durable child-budget state transitions.

No business dispatcher, evaluator, Python backend, or world is accepted here.
The append callback must persist an event before returning. A failed append
poisons the owner even when it is uncertain whether the event reached storage.
"""

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import re
import stat
import uuid

from .ptc_config import normalize_ptc_resources, PTC_PROTOCOL


GRANT_BYTES = 4096
_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}\Z")
_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,95}\Z")
_GRANT_FIELDS = {"protocol", "bundle_id", "episode_id", "parent_id", "top_level_index",
                 "prior_nested", "allowance", "max_tool_calls", "mode", "deadline_monotonic"}


class PTCBudgetError(RuntimeError):
    pass


def validate_grant(grant, config):
    config = normalize_ptc_resources(config)
    if type(grant) is not dict or set(grant) != _GRANT_FIELDS or grant["protocol"] != PTC_PROTOCOL:
        raise PTCBudgetError("PTC_GRANT_INVALID")
    if (type(grant["deadline_monotonic"]) not in (int, float)
            or not math.isfinite(grant["deadline_monotonic"]) or grant["deadline_monotonic"] <= 0):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    if any(type(grant[k]) is not str or _ID.fullmatch(grant[k]) is None
           for k in ("bundle_id", "episode_id", "parent_id")):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    if any(type(grant[k]) is not int for k in ("top_level_index", "prior_nested", "allowance", "max_tool_calls")):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    if (not 1 <= grant["top_level_index"] <= grant["max_tool_calls"] <= 100000
            or not 0 <= grant["prior_nested"] <= config["max_children_per_episode"]):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    remaining = min(grant["max_tool_calls"] - grant["top_level_index"] - grant["prior_nested"],
                    config["max_children_per_program"],
                    config["max_children_per_episode"] - grant["prior_nested"])
    if grant["mode"] not in ("live", "reconstruct"):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    expected = remaining if grant["mode"] == "live" else 0
    if expected < 0 or grant["allowance"] != expected:
        raise PTCBudgetError("PTC_GRANT_BUDGET_MISMATCH")
    return dict(grant)


def make_grant(*, bundle_id, episode_id, parent_id, top_level_index, prior_nested,
               max_tool_calls, config, deadline_monotonic, mode="live"):
    config = normalize_ptc_resources(config)
    if any(type(v) is not int for v in (top_level_index, prior_nested, max_tool_calls)):
        raise PTCBudgetError("PTC_GRANT_INVALID")
    allowance = min(max_tool_calls - top_level_index - prior_nested,
                    config["max_children_per_program"], config["max_children_per_episode"] - prior_nested)
    return validate_grant({"protocol": PTC_PROTOCOL, "bundle_id": bundle_id, "episode_id": episode_id,
                           "parent_id": parent_id, "top_level_index": top_level_index,
                           "prior_nested": prior_nested, "max_tool_calls": max_tool_calls,
                           "deadline_monotonic": deadline_monotonic,
                           "allowance": allowance if mode == "live" else 0, "mode": mode}, config)


def _sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class GrantStore:
    """One controller/MCP pair owns this private, unmounted directory.

    Consumed grants remain as bounded receipts. A hard-link claim is atomic and
    cannot overwrite an earlier consumer, unlike an unrestricted rename.
    """

    def __init__(self, root, config):
        self.root = Path(root)
        self.config = normalize_ptc_resources(config)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        info = self.root.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise PTCBudgetError("PTC_GRANT_DIRECTORY_NOT_PRIVATE")

    def _paths(self, grant):
        index = grant["top_level_index"]
        return self.root / f"{index}.grant", self.root / f"{index}.consumed"

    def issue(self, grant):
        grant = validate_grant(grant, self.config)
        pending, consumed = self._paths(grant)
        if os.path.lexists(consumed):
            raise PTCBudgetError("PTC_GRANT_ALREADY_CONSUMED")
        payload = json.dumps(grant, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("ascii")
        if len(payload) > GRANT_BYTES:
            raise PTCBudgetError("PTC_GRANT_INVALID")
        temporary = self.root / (".grant-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.link(temporary, pending, follow_symlinks=False)
            temporary.unlink()
            _sync_directory(self.root)
        except OSError as exc:
            raise PTCBudgetError("PTC_GRANT_ISSUE_UNKNOWN") from exc
        finally:
            temporary.unlink(missing_ok=True)

    def consume(self, expected):
        expected = validate_grant(expected, self.config)
        pending, consumed = self._paths(expected)
        try:
            os.link(pending, consumed, follow_symlinks=False)
            pending.unlink()
            _sync_directory(self.root)
            fd = os.open(consumed, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as handle:
                info = os.fstat(handle.fileno())
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != os.getuid()
                        or info.st_mode & 0o077 or info.st_size > GRANT_BYTES):
                    raise PTCBudgetError("PTC_GRANT_INVALID")
                raw = handle.read(GRANT_BYTES + 1)
            if len(raw) > GRANT_BYTES:
                raise PTCBudgetError("PTC_GRANT_INVALID")
            def unique(pairs):
                value = {}
                for key, item in pairs:
                    if key in value:
                        raise ValueError("Duplicate grant key")
                    value[key] = item
                return value
            actual = validate_grant(json.loads(raw, object_pairs_hook=unique), self.config)
            if actual != expected:
                raise PTCBudgetError("PTC_GRANT_CONTEXT_MISMATCH")
            return actual
        except (OSError, ValueError, UnicodeError, RecursionError) as exc:
            raise PTCBudgetError("PTC_GRANT_CONSUME_FAILED") from exc


class ParentBudget:
    """A single active child; a stopped or uncertain owner can never dispatch.

    Intent is not proof of execution. Only a complete recorded result confirms
    dispatch here; missing results retain a separate unknown-intent count.
    """

    def __init__(self, grant, config, append):
        self.config = normalize_ptc_resources(config)
        self.grant = validate_grant(grant, self.config)
        if self.grant["mode"] != "live":
            raise PTCBudgetError("PTC_RECONSTRUCTION_HAS_NO_LIVE_GRANT")
        if not callable(append):
            raise TypeError("A durable controller append function is required")
        self._append = append
        self._events = []
        self.admitted = 0
        self.dispatched = 0
        self.dispatch_intents = 0
        self.rejections = 0
        self.exhaustions = 0
        self.bridge_bytes = 0
        self.active = None
        self.stopped = False
        self.failed = False

    @property
    def events(self):
        return deepcopy(self._events)

    def _commit(self, event):
        if self.failed:
            raise PTCBudgetError("PTC_JOURNAL_UNKNOWN")
        try:
            self._append(deepcopy(event))
        except Exception:
            self.failed = self.stopped = True
            raise
        self._events.append(deepcopy(event))

    def _open(self):
        if self.stopped or self.failed:
            raise PTCBudgetError("PTC_BRIDGE_CLOSED")

    def _code(self, code):
        if type(code) is not str or _CODE.fullmatch(code) is None:
            raise PTCBudgetError("PTC_ERROR_CODE_INVALID")
        return code

    def admit(self, request):
        from .ptc_protocol import encode_frame
        self._open()
        if self.active is not None:
            raise PTCBudgetError("PTC_CHILD_ALREADY_ACTIVE")
        seq = self.admitted + 1
        if self.admitted >= self.grant["allowance"]:
            self._commit({"event": "exhausted", "seq": seq})
            self.exhaustions += 1
            self.stopped = True
            raise PTCBudgetError("PTC_CHILD_BUDGET_EXHAUSTED")
        try:
            frame = encode_frame({"type": "child_request", "seq": seq, "payload": request},
                                 max_bytes=self.config["max_request_bytes"],
                                 max_depth=self.config["max_json_depth"], max_nodes=self.config["max_json_nodes"])
        except ValueError:
            self.stop("REQUEST_NOT_RECORDABLE")
            raise
        # Reserve the entire permitted response before admitting the request.
        if self.bridge_bytes + len(frame) + self.config["max_response_bytes"] > self.config["max_bridge_bytes"]:
            self.stop("BRIDGE_CAPACITY")
            raise PTCBudgetError("PTC_BRIDGE_CAPACITY_EXHAUSTED")
        self._commit({"event": "admit", "seq": seq, "request": deepcopy(request), "bytes": len(frame)})
        self.admitted += 1
        self.bridge_bytes += len(frame)
        self.active = "admitted"
        return seq

    def reject(self, code, output=None):
        self._open()
        if self.active != "admitted":
            raise PTCBudgetError("PTC_REJECTION_ORDER_INVALID")
        code = self._code(code)
        payload = output if output is not None else {"ok": False, "error": {"code": code}}
        if (type(payload) is not dict or payload.get("ok") is not False
                or type(payload.get("error")) is not dict or payload["error"].get("code") != code):
            raise PTCBudgetError("PTC_REJECTION_OUTPUT_INVALID")
        try:
            frame = self._response(payload)
        except ValueError:
            self.cancel_before_dispatch("REJECTION_NOT_RECORDABLE")
            raise
        self._commit({"event": "reject", "seq": self.admitted, "code": code,
                      "output": payload, "bytes": len(frame)})
        self.bridge_bytes += len(frame)
        self.rejections += 1
        self.active = None
        return payload

    def dispatch_intent(self):
        self._open()
        if self.active != "admitted":
            raise PTCBudgetError("PTC_DISPATCH_ORDER_INVALID")
        self._commit({"event": "dispatch_intent", "seq": self.admitted})
        self.dispatch_intents += 1
        self.active = "dispatched"

    def cancel_before_dispatch(self, code):
        self._open()
        if self.active != "admitted":
            raise PTCBudgetError("PTC_CANCEL_ORDER_INVALID")
        self._commit({"event": "cancel", "seq": self.admitted, "code": self._code(code)})
        self.active = None
        self.stopped = True

    def _response(self, result):
        from .ptc_protocol import encode_frame
        return encode_frame({"type": "child_response", "seq": self.admitted, "payload": result},
                            max_bytes=self.config["max_response_bytes"],
                            max_depth=self.config["max_json_depth"], max_nodes=self.config["max_json_nodes"])

    def complete(self, result):
        self._open()
        if self.active != "dispatched":
            raise PTCBudgetError("PTC_RESULT_ORDER_INVALID")
        try:
            frame = self._response(result)
        except ValueError:
            self.unknown("RESULT_NOT_RECORDABLE")
            raise
        self._commit({"event": "result", "seq": self.admitted, "result": result, "bytes": len(frame)})
        self.bridge_bytes += len(frame)
        self.dispatched += 1
        self.active = None
        return deepcopy(result)

    def unknown(self, code):
        self._open()
        if self.active != "dispatched":
            raise PTCBudgetError("PTC_UNKNOWN_ORDER_INVALID")
        self._commit({"event": "unknown", "seq": self.admitted, "code": self._code(code)})
        self.stopped = True

    def stop(self, reason):
        self._open()
        if self.active is not None:
            raise PTCBudgetError("PTC_ACTIVE_CHILD_CANNOT_FINISH")
        self._commit({"event": "stop", "seq": self.admitted + 1, "code": self._code(reason)})
        self.stopped = True

    def accounting(self):
        return {"nested_requests_admitted": self.admitted,
                "nested_dispatch_intents": self.dispatch_intents,
                "nested_business_dispatched": self.dispatched,
                "nested_dispatch_unknown": self.dispatch_intents - self.dispatched,
                "nested_rejections": self.rejections, "bridge_exhaustion_events": self.exhaustions,
                "bridge_bytes": self.bridge_bytes, "stopped": self.stopped,
                "journal_unknown": self.failed}


def validate_child_events(grant, config, events, *, require_terminal=True):
    """Pure replay of budget transitions, with no business or Python authority."""
    if type(events) is not list:
        raise PTCBudgetError("PTC_CHILD_EVENTS_INVALID")
    config = normalize_ptc_resources(config)
    if len(events) > config["max_children_per_program"] * 3 + 1:
        raise PTCBudgetError("PTC_CHILD_EVENTS_TOO_MANY")
    owner = ParentBudget(grant, config, lambda event: None)
    for row in events:
        from .ptc_protocol import bounded_json_bytes
        bounded_json_bytes(row,
            max_bytes=config["max_request_bytes"] + config["max_response_bytes"] + 8192,
            max_depth=config["max_json_depth"] + 4, max_nodes=config["max_json_nodes"] + 64)
        if type(row) is not dict or type(row.get("event")) is not str:
            raise PTCBudgetError("PTC_CHILD_EVENT_INVALID")
        before = len(owner._events)
        kind = row["event"]
        try:
            if kind == "admit":
                owner.admit(row["request"])
            elif kind == "reject":
                owner.reject(row["code"], row["output"])
            elif kind == "dispatch_intent":
                owner.dispatch_intent()
            elif kind == "cancel":
                owner.cancel_before_dispatch(row["code"])
            elif kind == "result":
                owner.complete(row["result"])
            elif kind == "unknown":
                owner.unknown(row["code"])
            elif kind == "stop":
                owner.stop(row["code"])
            elif kind == "exhausted":
                try:
                    owner.admit({})
                except PTCBudgetError as exc:
                    if str(exc) != "PTC_CHILD_BUDGET_EXHAUSTED":
                        raise
            else:
                raise PTCBudgetError("PTC_CHILD_EVENT_INVALID")
        except KeyError as exc:
            raise PTCBudgetError("PTC_CHILD_EVENT_INVALID") from exc
        # JSON equality alone considers True == 1; require exact scalar types.
        if (len(owner._events) != before + 1
                or json.dumps(owner._events[-1], sort_keys=True, allow_nan=False) !=
                   json.dumps(row, sort_keys=True, allow_nan=False)):
            raise PTCBudgetError("PTC_CHILD_EVENT_MISMATCH")
    if require_terminal and (not owner.stopped or owner.active is not None):
        raise PTCBudgetError("PTC_CHILD_EVENTS_INCOMPLETE")
    return owner.accounting()
