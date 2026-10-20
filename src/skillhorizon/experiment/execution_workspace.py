"""Ordered file and Python effects for a controller-owned episode workspace.

This is an execution layer, not an agent controller. Prefix replay never becomes
live merely by exhausting records: the trusted controller must release it after
independent reconstruction checks and admission. Not a public tool method.
"""

from copy import deepcopy
import math
import os
import shutil
import sys
import time
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory

from .ptc_budget import ParentBudget, validate_child_events, validate_grant
from .ptc_config import EPISODE_STORAGE_CEILING, normalize_ptc_resources, ptc_storage_bounds
from .ptc_protocol import bounded_json_bytes
from .python_execution import DockerPython, PythonExecutionError, frame_bound, normalize_python_execution, validate_guest_result
from .workspace import WorkspaceRuntime, WorkspaceFatalError, workspace_entries
from .workspace_state import workspace_snapshot, restore_snapshot, validate_snapshot
from ..util import canonical_json


def _rejection(code):
    messages = {"ARGUMENT_SCHEMA": "Supply exactly one valid UTF-8 code string without NUL.",
                "CODE_LIMIT": "Code exceeds the declared UTF-8 byte limit.",
                "EXECUTION_CALL_LIMIT": "The declared Python execution call budget is exhausted."}
    return {"ok": False, "error": {"code": code, "message": messages[code]}}


def _bounded_json_size(value, limit, *, nodes, depth=12):
    """Bound plain JSON before serialization/copying, including malformed inputs."""
    remaining, remaining_nodes = limit, nodes

    def visit(item, level):
        nonlocal remaining, remaining_nodes
        remaining_nodes -= 1
        if remaining_nodes < 0 or level > depth:
            raise ValueError("JSON structure exceeds the declared record envelope")
        kind = type(item)
        if kind is str:
            if len(item) > remaining:
                raise ValueError("String exceeds the declared record envelope")
            size = 2
            for char in item:
                number = ord(char)
                size += (12 if number > 65535 else 6 if number >= 127 or number < 32
                         else 2 if char in ('"', "\\") else 1)
        elif kind in (dict, list):
            if len(item) > remaining_nodes:
                raise ValueError("Container exceeds the declared record envelope")
            size = 2 + 2 * len(item)
        elif item is None or kind is bool:
            size = 5
        elif kind is int:
            digits = item.bit_length() * 1234 // 4096 + 1
            if digits > min(4096, sys.get_int_max_str_digits() or 4096):
                raise ValueError("Integer exceeds the declared record envelope")
            size = digits + int(item < 0)
        elif kind is float and math.isfinite(item):
            size = 32
        else:
            raise ValueError("Record requires bounded plain JSON values")
        remaining -= size
        if remaining < 0:
            raise ValueError("JSON exceeds the declared record envelope")
        if kind is dict:
            for key, child in item.items():
                if type(key) is not str:
                    raise ValueError("Record keys must be strings")
                visit(key, level + 1)
                visit(child, level + 1)
        elif kind is list:
            for child in item:
                visit(child, level + 1)

    visit(value, 0)
    return limit - remaining


class _BoundedFileRuntime(WorkspaceRuntime):
    def __init__(self, root, journal, config, validate_record):
        self.validate_record = validate_record
        super().__init__(root, journal, config)

    def _append(self, name, arguments, output, *, execution=None):
        record = {"name": name, "arguments": arguments, "output": output}
        if execution is not None:
            record["execution"] = execution
        self.validate_record(record)
        super()._append(name, arguments, output, execution=execution)


class ExecutionWorkspace:
    """Controller supplies max_actions from its frozen total tool-call budget.

    actions counts admitted attempts (including rejections/failures); records
    counts confirmed journal appends. calls charges non-rejected Python/PTC dispatch
    and validated historical executions, even if the later file commit fails.
    backend_invocations is local dispatch count, split into confirmed physical
    executions and attempts whose physical outcome is unknown. Replay increments
    neither. Resource totals include only validated observations, not estimates.
    Pending receipts are last-known observations, never crash-recovery proof of
    zero execution or replay authority. The pending marker always wins.
    """

    def __init__(self, root, journal, workspace, execution, *, max_actions,
                 backend=None, records=None, replay_mode=None, ptc=None):
        self.files = _BoundedFileRuntime(root, journal, workspace, lambda row: self._check_record(row))
        self.config = normalize_python_execution(execution, self.files.config)
        if self.config is None:
            raise ValueError("Execution workspace requires an explicit Python declaration")
        if type(max_actions) is not int or not 1 <= max_actions <= 100000:
            raise ValueError("An explicit bounded controller max_actions is required")
        self.max_actions = max_actions
        self.ptc = normalize_ptc_resources(ptc) if ptc is not None else None
        cfg = self.files.config
        path_bytes = cfg["max_depth"] * 256
        self.argument_bound = 6 * (2 * cfg["max_file_bytes"] + 2 * path_bytes
                                  + self.config["max_code_bytes"]) + 4096
        self.output_bound = max(6 * cfg["max_response_chars"], 12 * self.config["max_stream_bytes"] + 4096)
        self.action_record_bound = self.argument_bound + self.output_bound + 1024
        snapshots_bound = 2 * frame_bound(cfg, self.config)
        self.record_bound = self.action_record_bound + snapshots_bound
        self.journal_bound = (max_actions * self.action_record_bound
                             + min(max_actions, self.config["max_calls"]) * snapshots_bound)
        self.receipt_bound = self.action_record_bound + 8192
        if self.ptc is not None:
            bounds = ptc_storage_bounds(self.ptc, cfg, self.config, max_actions)
            self.record_bound = bounds["record_bytes"]
            self.journal_bound = bounds["journal_bytes"]
            self.receipt_bound = bounds["receipt_bytes"]
        if self.journal_bound + self.receipt_bound > EPISODE_STORAGE_CEILING:
            raise ValueError("Action journal and receipt exceed the supported episode bound")
        self.record_nodes = 32 * cfg["max_entries"] + 256
        if self.files.journal.exists() and self.files.journal.stat().st_size:
            raise ValueError("Execution journal must start fresh; reconstruct through ordered replay")
        if (records is None) != (replay_mode is None) or replay_mode not in (None, "full", "prefix"):
            raise ValueError("Replay requires an explicit full or prefix mode and records")
        if records is not None and type(records) is not list:
            raise ValueError("Replay records must be an ordered list")
        if records is not None:
            if len(records) > max_actions:
                raise ValueError("Replay exceeds the action count bound")
            total = started = 0
            for row in records:
                size, is_started = self._check_record(row)
                total += size + 1
                started += is_started
                if total > self.journal_bound or started > self.config["max_calls"]:
                    raise ValueError("Replay exceeds the execution or journal bound")
        self.records = deepcopy(records)
        self.mode = replay_mode
        self.index = 0
        # Validators must not even construct an execution backend.
        self._backend = backend
        self.pending = self.files.journal.with_name(self.files.journal.name + ".execution_pending")
        self.receipt = self.files.journal.with_name(self.files.journal.name + ".execution_receipt")
        self.file_names = {name for name, _, _ in workspace_entries()}
        self.failed = False
        self.usage = {"actions": 0, "records": 0, "calls": 0, "backend_invocations": 0,
                      "physical_executions": 0, "unknown_execution_attempts": 0, "wall_seconds": 0.0,
                      "truncated_streams": 0, "captured_bytes": 0}
        self.current = None
        self._last_ptc = None
        self._parent_ids = set()
        self._active_budget = None
        if self.ptc is not None:
            self.usage.update({key: 0 for key in (
                "ptc_calls", "nested_requests_admitted", "nested_dispatch_intents",
                "nested_business_dispatched", "nested_dispatch_unknown", "nested_rejections",
                "bridge_exhaustion_events", "bridge_bytes")})
        self.journal_bytes = 0
        self.failure_receipt_persisted = False
        self._known()
        if self.ptc is not None and records is not None:
            self.validate_sequence(records)

    @property
    def backend(self):
        """Explicit live preflight access stays compatible; replay never uses it."""
        if self._backend is None:
            self._backend = DockerPython(self.config, self.files.config)
        return self._backend

    @backend.setter
    def backend(self, value):
        self._backend = value

    def _check_record(self, row):
        if type(row) is not dict or len(row) not in (3, 4):
            raise ValueError("Malformed action record")
        if row.get("name") == "ptc_execute":
            return self._check_ptc_record(row)
        _bounded_json_size(row, self.record_bound, nodes=self.record_nodes)
        if set(row) not in ({"name", "arguments", "output"}, {"name", "arguments", "output", "execution"}):
            raise ValueError("Malformed action record")
        if type(row["name"]) is not str or len(row["name"]) > 64:
            raise ValueError("Malformed action name")
        _bounded_json_size(row["arguments"], self.argument_bound, nodes=64, depth=4)
        _bounded_json_size(row["output"], self.output_bound, nodes=self.record_nodes)
        started = False
        if "execution" in row:
            effects = row["execution"]
            if row["name"] != "python_execute" or type(effects) is not dict or type(effects.get("started")) is not bool:
                raise ValueError("Malformed execution effects")
            started = effects["started"]
            if set(effects) != ({"started", "before", "after"} if started else {"started"}):
                raise ValueError("Malformed execution effects")
        elif row["name"] == "python_execute":
            raise ValueError("Missing execution effects")
        bound = self.record_bound if started else self.action_record_bound
        return _bounded_json_size(row, bound, nodes=self.record_nodes), started

    def _validate_parent(self, parent):
        fields = {"grant", "turn", "actor_turn", "workspace_index"}
        if self.ptc is None or type(parent) is not dict or set(parent) != fields:
            raise ValueError("PTC requires an exact trusted parent context")
        _bounded_json_size(parent, 8192, nodes=128)
        grant = validate_grant(parent["grant"], self.ptc)
        if grant["mode"] != "live" or grant["max_tool_calls"] != self.max_actions:
            raise ValueError("PTC parent grant differs from the frozen declaration")
        if any(type(parent[k]) is not int or not 1 <= parent[k] <= 100000
               for k in ("turn", "actor_turn", "workspace_index")):
            raise ValueError("PTC native positions must be positive integers")
        return grant

    def _check_ptc_record(self, row, *, partial=False):
        if (self.ptc is None or set(row) != {"name", "arguments", "output", "execution"}
                or row["name"] != "ptc_execute"):
            raise ValueError("PTC record requires its explicit declaration and effects")
        effects = row["execution"]
        if type(effects) is not dict or type(effects.get("started")) is not bool:
            raise ValueError("Malformed PTC execution effects")
        fields = {"started", "ptc"} | ({"before", "after"} if effects["started"] else set())
        if set(effects) != fields and not (partial and set(effects) in (
                fields - {"after"}, fields - {"before", "after"})):
            raise ValueError("Malformed PTC file effects")
        ptc = effects["ptc"]
        if type(ptc) is not dict or set(ptc) != {"parent", "events"}:
            raise ValueError("Malformed PTC parent effects")
        grant = self._validate_parent(ptc["parent"])
        events = ptc["events"]
        accounting = validate_child_events(grant, self.ptc, events, require_terminal=not partial)
        # E1 measures business JSON in its own envelope, never the D file limits.
        event_bytes = sum(len(bounded_json_bytes(event,
            max_bytes=self.ptc["max_request_bytes"] + self.ptc["max_response_bytes"] + 8192,
            max_depth=self.ptc["max_json_depth"] + 4,
            max_nodes=self.ptc["max_json_nodes"] + 64)) for event in events)
        shell = {**row, "execution": {**effects, "ptc": {**ptc, "events": []}}}
        _bounded_json_size(row["arguments"], self.argument_bound, nodes=64, depth=4)
        _bounded_json_size(row["output"], self.output_bound, nodes=self.record_nodes)
        _bounded_json_size(shell, self.record_bound, nodes=self.record_nodes + 128)
        size = len(canonical_json(shell)) + event_bytes + max(0, len(events) - 1)
        if size + 1 > self.record_bound:
            raise ValueError("PTC record exceeds its combined storage envelope")
        if not partial:
            if accounting["nested_dispatch_unknown"] or accounting["journal_unknown"]:
                raise ValueError("Unknown PTC child observations cannot be replayed")
            if effects["started"]:
                pre = validate_snapshot(effects["before"], self.files.config)
                after = validate_snapshot(effects["after"], self.files.config)
                output = row["output"]
                if type(output) is not dict or set(output) != {"ok", "data"} or output["ok"] is not True:
                    raise ValueError("Invalid recorded PTC public result")
                validate_guest_result({"valid": True, "error_code": None,
                    "tree": after, "result": output["data"]}, pre, self.files.config, self.config)
            else:
                output = row["output"]
                error = output.get("error") if type(output) is dict else None
                code = error.get("code") if type(error) is dict else None
                if (code not in ("ARGUMENT_SCHEMA", "CODE_LIMIT", "EXECUTION_CALL_LIMIT")
                        or canonical_json(output) != canonical_json(_rejection(code))
                        or events != [{"event": "stop", "seq": 1, "code": code}]):
                    raise ValueError("PTC rejection lacks its bound terminal event")
        return size, effects["started"]

    def validate_record(self, row):
        """Pure bounded typed validation; ordering is checked by validate_sequence."""
        self._check_record(row)
        return deepcopy(row)

    def validate_sequence(self, records):
        """Validate all records against the current initial tree, without backends.

        This proves only structural/file reconstruction, never business replay.
        It does not mutate this consumer, its usage, or its live workspace.
        """
        self._known()
        if type(records) is not list or len(records) > self.max_actions:
            raise ValueError("Replay exceeds the action count bound")
        total = calls = 0
        for row in records:
            size, started = self._check_record(row)
            total += size + 1
            calls += started
            if total > self.journal_bound or calls > self.config["max_calls"]:
                raise ValueError("Replay exceeds the execution or journal bound")
        with TemporaryDirectory(prefix="execution-sequence-") as directory:
            root = Path(directory)
            restore_snapshot(root / "files", workspace_snapshot(self.files.root, self.files.config), self.files.config)
            consumer = ExecutionWorkspace(root / "files", root / "actions.jsonl", self.files.config,
                self.config, max_actions=self.max_actions, ptc=self.ptc)
            consumer.records, consumer.mode = deepcopy(records), "full"
            for row in records:
                consumer.call(row["name"], row["arguments"])
            consumer.finish_replay()
            return {"structure_complete": True, "usage": deepcopy(consumer.usage),
                    "tree": workspace_snapshot(consumer.files.root, consumer.files.config)}

    def apply_validated_parent_files(self, record):
        """Consume the next historical PTC row only; no business authority here."""
        self._known()
        if self.mode is None or type(record) is not dict or record.get("name") != "ptc_execute":
            raise ValueError("PTC file application requires an ordered historical parent")
        self._check_record(record)
        if self.index >= len(self.records) or canonical_json(record) != canonical_json(self.records[self.index]):
            raise ValueError("PTC parent differs from the next historical record")
        return self.call(record["name"], record["arguments"])

    def _known(self):
        if self.failed or os.path.lexists(self.pending):
            raise WorkspaceFatalError("Execution file state is unknown; continuation is forbidden")

    def _mark_pending(self):
        self.pending.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write("Execution has not committed a verified complete file state.\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _write_receipt(self, state):
        value = {"state": state, "replayable": False, "action": self.current, "usage": self.usage}
        if self.current is not None and self.current.get("name") == "ptc_execute":
            size, _ = self._check_ptc_record(self.current, partial=True)
            size += _bounded_json_size({**value, "action": None}, 16384, nodes=256)
            if size > self.receipt_bound:
                raise ValueError("PTC receipt exceeds its combined storage envelope")
        else:
            _bounded_json_size(value, self.receipt_bound, nodes=self.record_nodes)
        payload = canonical_json(value) + "\n"
        temporary = self.receipt.with_name(self.receipt.name + "." + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w", encoding="ascii") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.receipt)
            if self.ptc is not None:
                fd = os.open(self.receipt.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def _replace_tree(self, before, after):
        if workspace_snapshot(self.files.root, self.files.config) != before:
            raise WorkspaceFatalError("Workspace changed before recorded effects could be applied")
        stage = self.files.root.parent / (".execution-stage-" + uuid.uuid4().hex)
        stage.mkdir(mode=0o700)
        # Preserve a failed stage for the controller; never claim rollback after
        # an interrupted replacement or silently discard the only current tree.
        restore_snapshot(stage / "new", after, self.files.config)
        os.rename(self.files.root, stage / "old")
        os.rename(stage / "new", self.files.root)
        if workspace_snapshot(self.files.root, self.files.config) != after:
            raise WorkspaceFatalError("Applied execution state differs from the verified result")
        shutil.rmtree(stage)

    def _code_rejection(self, arguments):
        if type(arguments) is not dict or set(arguments) != {"code"} or type(arguments["code"]) is not str:
            return _rejection("ARGUMENT_SCHEMA")
        try:
            encoded = arguments["code"].encode("utf-8")
        except UnicodeError:
            return _rejection("ARGUMENT_SCHEMA")
        if b"\0" in encoded:
            return _rejection("ARGUMENT_SCHEMA")
        if len(encoded) > self.config["max_code_bytes"]:
            return _rejection("CODE_LIMIT")
        if self.usage["calls"] >= self.config["max_calls"]:
            return _rejection("EXECUTION_CALL_LIMIT")
        return None

    def _execution(self, arguments, recorded):
        rejection = self._code_rejection(arguments)
        before = workspace_snapshot(self.files.root, self.files.config) if rejection is None else None
        if recorded is not None:
            self._check_record(recorded)
            effects = recorded["execution"]
            if effects["started"] != (rejection is None):
                raise ValueError("Execution record budget differs")
            output = deepcopy(recorded["output"])
            if rejection is not None:
                if canonical_json(output) != canonical_json(rejection):
                    raise ValueError("Execution rejection differs from reconstruction")
            else:
                pre = validate_snapshot(effects["before"], self.files.config)
                after = validate_snapshot(effects["after"], self.files.config)
                if pre != before:
                    raise ValueError("Execution record prestate differs")
                if type(output) is not dict or set(output) != {"ok", "data"} or output["ok"] is not True:
                    raise ValueError("Invalid recorded public execution result")
                after, public = validate_guest_result(
                    {"valid": True, "error_code": None, "tree": after, "result": output["data"]},
                    before, self.files.config, self.config)
                self.usage["calls"] += 1
        elif rejection is not None:
            output = rejection
        else:
            self.usage["calls"] += 1
            self.usage["backend_invocations"] += 1
            self.usage["unknown_execution_attempts"] += 1
            backend_error = None
            try:
                result = self.backend.execute(before, arguments["code"])
            except PythonExecutionError as exc:
                if exc.execution_evidence is None:
                    raise
                backend_error, result = exc, exc.execution_evidence
            if type(result) is not dict:
                raise ValueError("Invalid backend result")
            if type(result.get("physical_executions")) is int and result["physical_executions"] == 1:
                self.usage["physical_executions"] += 1
                self.usage["unknown_execution_attempts"] -= 1
            # Preserve validated public resources even if cleanup/tree validation fails.
            _bounded_json_size(result.get("result"), self.output_bound, nodes=64)
            _, public = validate_guest_result(
                {"valid": True, "error_code": None, "tree": before, "result": result.get("result")},
                before, self.files.config, self.config)
            output = {"ok": True, "data": public}
            self._account_public(output)
            if backend_error is not None:
                raise backend_error
            if result.get("container_removed") is not True or self.usage["unknown_execution_attempts"]:
                raise WorkspaceFatalError("Execution cleanup or physical execution count is unverified")
            _bounded_json_size(result.get("tree"), self.record_bound, nodes=self.record_nodes)
            after, _ = validate_guest_result(
                {"valid": True, "error_code": None, "tree": result.get("tree"), "result": public},
                before, self.files.config, self.config)
        effects = {"started": rejection is None}
        if rejection is None:
            if recorded is not None:
                self._account_public(output)
            self.usage["captured_bytes"] += sum(row.get("bytes", 0) for row in after.values())
            effects.update(before=before, after=after)
        record = {"name": "python_execute", "arguments": arguments, "output": output, "execution": effects}
        self._check_record(record)
        self.current["output"] = output
        self._write_receipt("pending")
        if rejection is None:
            self._replace_tree(before, after)
        self.files._append("python_execute", arguments, output, execution=effects)
        return output

    def _bind_ptc_position(self, parent):
        grant = self._validate_parent(parent)
        index = self.usage["actions"]
        if (parent["workspace_index"] != index or grant["top_level_index"] < index
                or grant["prior_nested"] != self.usage["nested_requests_admitted"]
                or grant["parent_id"] in self._parent_ids):
            raise ValueError("PTC parent position or prior nested accounting differs")
        if self._last_ptc is not None:
            previous = self._last_ptc
            old = previous["grant"]
            if (any(grant[k] != old[k] for k in ("bundle_id", "episode_id"))
                    or grant["top_level_index"] - old["top_level_index"] < index - previous["workspace_index"]
                    or parent["turn"] < previous["turn"] or parent["actor_turn"] < previous["actor_turn"]):
                raise ValueError("PTC parent identity or native ordering differs")
        self._last_ptc = deepcopy(parent)
        self._parent_ids.add(grant["parent_id"])

    def _ptc_execution(self, arguments, recorded, parent, backend):
        rejection = self._code_rejection(arguments)
        if recorded is not None:
            effects = deepcopy(recorded["execution"])
            parent = effects["ptc"]["parent"]
        else:
            effects = {"started": rejection is None, "ptc": {"parent": deepcopy(parent), "events": []}}
        self.current["execution"] = effects
        self._bind_ptc_position(parent)
        self._write_receipt("pending")
        if effects["started"] != (rejection is None):
            raise ValueError("PTC record execution budget differs")
        keys = ("nested_requests_admitted", "nested_dispatch_intents", "nested_business_dispatched",
                "nested_dispatch_unknown", "nested_rejections", "bridge_exhaustion_events", "bridge_bytes")
        baseline = {key: self.usage[key] for key in keys}

        def account(counts):
            for key in keys:
                self.usage[key] = baseline[key] + counts[key]

        if recorded is not None:
            self._check_record(recorded)
            output = deepcopy(recorded["output"])
            if rejection is not None and canonical_json(output) != canonical_json(rejection):
                raise ValueError("PTC rejection differs from reconstruction")
            account(validate_child_events(parent["grant"], self.ptc, effects["ptc"]["events"]))
            if rejection is None:
                before = workspace_snapshot(self.files.root, self.files.config)
                if before != effects["before"]:
                    raise ValueError("PTC record prestate differs")
                self.usage["calls"] += 1
                self.usage["ptc_calls"] += 1
                self._account_public(output)
        else:
            def append(event):
                # ParentBudget calls us before changing its own counters. Persist
                # the proposed counters with the event before returning authority.
                effects["ptc"]["events"].append(event)
                counts = validate_child_events(parent["grant"], self.ptc,
                    effects["ptc"]["events"], require_terminal=False)
                account(counts)
                try:
                    self._write_receipt("pending")
                except Exception:
                    self.usage["ptc_journal_unknown"] = True
                    raise

            budget = ParentBudget(parent["grant"], self.ptc, append)
            self._active_budget = budget
            try:
                if rejection is not None:
                    budget.stop(rejection["error"]["code"])
                    output = rejection
                else:
                    if parent["grant"]["deadline_monotonic"] <= time.monotonic():
                        raise ValueError("PTC live deadline has expired")
                    effects["before"] = workspace_snapshot(self.files.root, self.files.config)
                    self.usage["calls"] += 1
                    self.usage["ptc_calls"] += 1
                    self._write_receipt("pending")
                    self.usage["backend_invocations"] += 1
                    self.usage["unknown_execution_attempts"] += 1
                    backend_error = None
                    try:
                        result = backend.execute(deepcopy(effects["before"]), arguments["code"],
                            budget=budget, deadline=parent["grant"]["deadline_monotonic"])
                    except PythonExecutionError as exc:
                        if exc.execution_evidence is None:
                            raise
                        backend_error, result = exc, exc.execution_evidence
                    if type(result) is not dict:
                        raise ValueError("Invalid PTC backend result")
                    if type(result.get("physical_executions")) is int and result["physical_executions"] == 1:
                        self.usage["physical_executions"] += 1
                        self.usage["unknown_execution_attempts"] -= 1
                    _bounded_json_size(result.get("result"), self.output_bound, nodes=64)
                    _, public = validate_guest_result({"valid": True, "error_code": None,
                        "tree": effects["before"], "result": result.get("result")},
                        effects["before"], self.files.config, self.config)
                    output = {"ok": True, "data": public}
                    self._account_public(output)
                    if backend_error is not None:
                        raise backend_error
                    if (result.get("container_removed") is not True
                            or self.usage["unknown_execution_attempts"] or budget.failed):
                        raise WorkspaceFatalError("PTC cleanup or physical execution is unverified")
                    if time.monotonic() >= parent["grant"]["deadline_monotonic"]:
                        raise WorkspaceFatalError("PTC parent deadline exceeded")
                    _bounded_json_size(result.get("tree"), self.record_bound, nodes=self.record_nodes)
                    after, _ = validate_guest_result({"valid": True, "error_code": None,
                        "tree": result.get("tree"), "result": public},
                        effects["before"], self.files.config, self.config)
                    effects["after"] = after
                    if not budget.stopped:
                        budget.stop("PROGRAM_COMPLETE")
                validate_child_events(parent["grant"], self.ptc, budget.events)
            except Exception:
                if not budget.stopped and not budget.failed:
                    if budget.active == "dispatched":
                        budget.unknown("PTC_PARENT_FAILED")
                    elif budget.active == "admitted":
                        budget.cancel_before_dispatch("PTC_PARENT_FAILED")
                    else:
                        budget.stop("PTC_PARENT_FAILED")
                raise
            finally:
                account(budget.accounting())
                if budget.failed:
                    self.usage["ptc_journal_unknown"] = True
                self._active_budget = None
        self.current["output"] = output
        self._check_record(self.current)
        self._write_receipt("pending")
        if rejection is None:
            self.usage["captured_bytes"] += sum(row.get("bytes", 0) for row in effects["after"].values())
            self._replace_tree(effects["before"], effects["after"])
        self.files._append("ptc_execute", arguments, output, execution=effects)
        return output

    def _account_public(self, output):
        self.current["output"] = output
        self.usage["wall_seconds"] += output["data"]["wall_seconds"]
        self.usage["truncated_streams"] += sum(output["data"][name + "_truncated"] for name in ("stdout", "stderr"))

    def call(self, name, arguments, *, ptc_parent=None, ptc_backend=None):
        """PTC live context is private and already consumed by ProductionExecution.

        parent = {grant, turn, actor_turn, workspace_index}; deadline comes only
        from grant["deadline_monotonic"], never from a newly started clock.
        positions are one-based. Grant carries native call_id as parent_id and
        native tool_index as top_level_index. No grant is issued/consumed here.
        Recorded calls accept neither context nor backend, in any replay mode.
        """
        self._known()
        recorded = None
        self.current = None
        try:
            names = self.file_names | {"python_execute"} | ({"ptc_execute"} if self.ptc is not None else set())
            if type(name) is not str or len(name) > 64 or name not in names:
                raise ValueError("Unknown file or execution tool")
            if name != "ptc_execute" or self.mode is not None:
                if ptc_parent is not None or ptc_backend is not None:
                    raise ValueError("Private PTC context is forbidden for ordinary or recorded calls")
            else:
                self._validate_parent(ptc_parent)
                if not callable(getattr(ptc_backend, "execute", None)):
                    raise ValueError("Live PTC requires an injected trusted backend")
            _bounded_json_size(arguments, self.argument_bound, nodes=64, depth=4)
            if self.usage["actions"] >= self.max_actions:
                raise ValueError("Controller action budget exhausted")
            if self.ptc is not None:
                minimum_top = self.usage["actions"] + 1
                if self._last_ptc is not None:
                    minimum_top += (self._last_ptc["grant"]["top_level_index"]
                                    - self._last_ptc["workspace_index"])
                if minimum_top + self.usage["nested_requests_admitted"] > self.max_actions:
                    raise ValueError("PTC shared charged budget exhausted")
            if self.mode is not None:
                if self.index >= len(self.records):
                    raise ValueError("Replay boundary is locked; no implicit live execution")
                recorded = self.records[self.index]
                if (type(recorded) is not dict or recorded.get("name") != name
                        or canonical_json(recorded.get("arguments")) != canonical_json(arguments)):
                    raise ValueError("Out-of-order or changed replay call")
            self.usage["actions"] += 1
            self.current = {"name": name, "arguments": arguments, "output": None}
            self._mark_pending()
            if name == "ptc_execute":
                output = self._ptc_execution(arguments, recorded, ptc_parent, ptc_backend)
                self.usage["records"] += 1
            elif name == "python_execute":
                self._write_receipt("pending")
                output = self._execution(arguments, recorded)
                self.usage["records"] += 1
            elif name in self.file_names:
                self._write_receipt("pending")
                if recorded is not None and set(recorded) != {"name", "arguments", "output"}:
                    raise ValueError("Malformed file action record")
                output = self.files.call(name, arguments)
                self.usage["records"] += 1
                self.current["output"] = output
                if recorded is not None and canonical_json(recorded["output"]) != canonical_json(output):
                    raise ValueError("File operation differs from ordered replay")
            self.journal_bytes = self.files.journal.stat().st_size
            if self.journal_bytes > self.journal_bound:
                raise ValueError("Journal exceeds the declared bound")
            self.current["output"] = output
            self._write_receipt("committed")
            self.pending.unlink()
            if recorded is not None:
                self.index += 1
            return output
        except Exception:
            self.failed = True
            try:
                if not os.path.lexists(self.pending):
                    self._mark_pending()
                self._write_receipt("failed")
                self.failure_receipt_persisted = True
            except Exception:
                self.failure_receipt_persisted = False
            suffix = "" if self.failure_receipt_persisted else "; failure receipt persistence is unconfirmed"
            raise WorkspaceFatalError("File or execution effects cannot be verified; stop the episode" + suffix) from None

    def finish_replay(self):
        self._known()
        if self.mode is None or self.index != len(self.records):
            raise ValueError("Replay is not complete")

    def authorize_live(self):
        """Controller-only, after full C boundary validation and admission."""
        self.finish_replay()
        if self.mode != "prefix":
            raise ValueError("A full replay cannot become live")
        self.mode = None
