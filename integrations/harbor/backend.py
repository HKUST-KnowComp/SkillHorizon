"""Private, serialized public-tool gateway and independent business verification."""

import argparse
from contextlib import contextmanager
from copy import deepcopy
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import threading
import time

from jsonschema import Draft202012Validator

from skillhorizon.experiment.tasks import load_task, validate_project_history
from skillhorizon.util import load_json, load_jsonl
from . import declarations


def write_record(path, value):
    path = Path(path)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False, sort_keys=True)
        stream.flush()
        import os
        os.fsync(stream.fileno())
    temporary.replace(path)


def encode_event(event):
    return (json.dumps(event, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n").encode("utf-8")


def append_event(path, event):
    with Path(path).open("ab") as stream:
        stream.write(encode_event(event))
        stream.flush()
        import os
        os.fsync(stream.fileno())


def require_full_online(root):
    root = Path(root)
    task = load_task(root)
    actions = load_jsonl(root / "checkpoint/model_skill_actions.jsonl")
    validate_project_history(task, actions)
    if task["checkpoint_action_count"] != 0 or actions:
        raise ValueError("Harbor v1 requires an unexecuted full-online checkpoint")
    if any(m.get("role") not in {"user", "system", "developer"}
           or m.get("type") not in {None, "message"}
           or not isinstance(m.get("content"), str) for m in task["messages"]):
        raise ValueError("Full-online task may not contain agent/tool history")
    return task


def make_runtime(task, checkpoint, runtime):
    from skillhorizon.experiment.internal_mcp import FORMAT
    if task["format"] == FORMAT:
        from integrations.internal_mcp.framework import RecordedRuntime, runtime_for
        return RecordedRuntime(runtime_for(task, checkpoint / "environment", runtime), runtime)
    from skillhorizon.experiment.project_runtime import project_runtime
    return project_runtime(task, runtime)


class GatewayError(Exception):
    def __init__(self, code, status=409):
        self.code, self.status = code, status
        super().__init__(code)


class BusinessGateway:
    def __init__(self, task_root, state_root, *, max_calls, create=True, require_code_origins=False):
        self.task_root, self.state_root = Path(task_root), Path(state_root)
        self.task = require_full_online(self.task_root)
        if type(max_calls) is not int or max_calls < 1:
            raise ValueError("max_calls must be a positive integer")
        self.max_calls = max_calls
        self.require_code_origins = require_code_origins
        self._source_monitor = None
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.control_path = self.state_root / "control.json"
        self.events_path = self.state_root / "gateway.jsonl"
        self.declarations_path = self.state_root / "declarations.jsonl"
        self.declaration_enabled = declarations.enabled(self.task)
        self._declaration_failed = False
        self.declaration_journal = (declarations.DeclarationJournal(self.declarations_path, self.task["skill_ids"])
                                    if self.declaration_enabled else None)
        self.runtime_root = self.state_root / "runtime"
        self._thread_lock = threading.Lock()
        self.tools = {tool["name"]: deepcopy(tool) for tool in self.task["tools"]}
        if len(self.tools) != len(self.task["tools"]):
            raise ValueError("Duplicate public tools")
        if self.declaration_enabled:
            self.tools.update({tool["name"]: deepcopy(tool) for tool in declarations.TOOLS})
        self.validators = {name: Draft202012Validator(tool["parameters"])
                           for name, tool in self.tools.items()}
        with self.locked():
            if not self.control_path.exists():
                if not create or self.runtime_root.exists() or self.events_path.exists() or self.declarations_path.exists():
                    raise ValueError("Missing gateway control; refusing to reconstruct execution")
                shutil.copytree(self.task_root / "checkpoint", self.runtime_root)
                self.events_path.touch(exist_ok=False)
                control = {"sealed": False, "execution_unknown": False,
                    "inflight": None, "attempts": 0, "dispatched": 0, "max_calls": max_calls}
                if self.declaration_enabled:
                    self.declarations_path.touch(exist_ok=False)
                    control["skill_protocol"] = declarations.PROTOCOL
                    control["declarations"] = {"attempts": 0, "event_index": 0,
                        "last_resolved_business_dispatch": 0, "pending": False, "invalid": False}
                    control["admission_rejections"] = {}
                write_record(self.control_path, control)
            control = load_json(self.control_path)
            if (control.get("skill_protocol") == declarations.PROTOCOL) != self.declaration_enabled:
                raise ValueError("Cannot change the skill protocol on restart")
            if control["max_calls"] != max_calls:
                raise ValueError("Cannot change the gateway budget on restart")
            if control["inflight"] is not None:
                control["execution_unknown"] = True
                write_record(self.control_path, control)
            if not self.events_path.is_file() or not self.runtime_root.is_dir():
                raise ValueError("Gateway records missing")
            if self.declaration_enabled:
                self._declaration_state(control)
        self.runtime = make_runtime(self.task, self.task_root / "checkpoint", self.runtime_root)

    def attest_sources(self, phase, *, publish=True, verify_files=True):
        from skillhorizon.experiment.environment_score import evaluate_environment
        from .provenance import attest
        actual = self.runtime.runtime if type(self.runtime).__name__ == "RecordedRuntime" else self.runtime
        required = {load_task.__module__, type(self.runtime).__module__,
                    type(actual).__module__, evaluate_environment.__module__}
        options = dict(required_modules=required, task_root=self.task_root,
                       runtime_root=self.runtime_root, entry_file=__file__)
        if phase == "serve":
            from .source_monitor import SourceMonitor
            if self._source_monitor is None:
                self._source_monitor = SourceMonitor(Path(__file__).resolve().parents[2], **options)
                report = self._source_monitor.report
            else:
                report = self._source_monitor.check(required_modules=required)
        else:
            report = attest(Path(__file__).resolve().parents[2], phase=phase,
                            verify_files=verify_files, **options)
        if publish:
            write_record(self.state_root / ("CODE_ORIGINS_" + phase.upper() + ".json"), report)
        return report

    @contextmanager
    def locked(self):
        with self._thread_lock, (self.state_root / "gateway.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _invalidate_declarations(self, control):
        self._declaration_failed = True
        if not isinstance(control.get("declarations"), dict):
            control["declarations"] = {}
        control["declarations"]["invalid"] = True
        try:
            write_record(self.control_path, control)
        except OSError:
            pass  # The process latch remains closed until invalid can be persisted.

    def _declaration_state(self, control, *, fresh=False):
        """A damaged declaration journal locks admission, not independent business scoring."""
        try:
            state = control["declarations"]
            if (self._declaration_failed or state["pending"] is not False or state["invalid"] is not False
                    or type(state["event_index"]) is not int
                    or state["event_index"] != control["attempts"] + state["attempts"]
                    or type(state["last_resolved_business_dispatch"]) is not int
                    or state["last_resolved_business_dispatch"] != control["dispatched"]):
                raise ValueError("Uncommitted declaration or business cut")
            journal = (declarations.DeclarationJournal(self.declarations_path, self.task["skill_ids"])
                       if fresh else self.declaration_journal)
            return journal.sync(state["attempts"])
        except (KeyError, TypeError, ValueError, OSError, RecursionError):
            self._invalidate_declarations(control)
            return None

    def _reject_admission(self, control, code, status):
        if self.declaration_enabled:
            counts = control["admission_rejections"]
            counts[code] = counts.get(code, 0) + 1
            write_record(self.control_path, control)
        raise GatewayError(code, status)

    def _declare(self, control, state, name, arguments):
        meta = control["declarations"]
        event = state.propose(name, arguments, meta["event_index"] + 1,
                              meta["last_resolved_business_dispatch"])
        if len(encode_event(event)) > declarations.MAX_EVENT_BYTES:
            self._reject_admission(control, "DECLARATION_EVENT_TOO_LARGE", 400)
        try:
            meta["pending"] = True
            write_record(self.control_path, control)
            append_event(self.declarations_path, event)
            # Re-read through the journal so its byte offset and state advance together.
            self.declaration_journal.sync(meta["attempts"] + 1)
            meta["attempts"] += 1
            meta["event_index"] += 1
            meta["pending"] = False
            write_record(self.control_path, control)
        except Exception:
            self._invalidate_declarations(control)
            raise GatewayError("DECLARATION_RECORDING_INVALID", 503) from None
        return event["receipt"]

    def call(self, name, arguments, *, skill_run_id=declarations.MISSING):
        with self.locked():
            control = load_json(self.control_path)
            if control["sealed"] or (self.state_root / "SEAL_STARTED.json").exists():
                raise GatewayError("SEALED")
            if control["execution_unknown"] or control["inflight"] is not None:
                raise GatewayError("EXECUTION_UNKNOWN")
            state = None
            if self.declaration_enabled:
                state = self._declaration_state(control)
                if state is None:
                    raise GatewayError("DECLARATION_RECORDING_INVALID", 503)
                if isinstance(name, str) and name in declarations.NAMES:
                    return self._declare(control, state, name, arguments)
            elif skill_run_id is not declarations.MISSING:
                raise GatewayError("INVALID_REQUEST", 400)
            if control["attempts"] >= self.max_calls:
                self._reject_admission(control, "BUSINESS_CALL_BUDGET", 429)
            control["attempts"] += 1
            sequence = control["attempts"]
            event = {"sequence": sequence, "name": name, "arguments": deepcopy(arguments)}
            if self.declaration_enabled:
                control["declarations"]["event_index"] += 1
                event.update(event_index=control["declarations"]["event_index"],
                             attribution=state.attribution(skill_run_id))
            invalid = (not isinstance(name, str) or name not in self.tools or
                       not isinstance(arguments, dict))
            if not invalid:
                invalid = not self.validators[name].is_valid(arguments)
            if invalid:
                write_record(self.control_path, control)
                append_event(self.events_path, {**event, "phase": "rejected", "code": "PUBLIC_SCHEMA"})
                raise GatewayError("PUBLIC_SCHEMA", 400)
            control["inflight"] = sequence
            control["dispatched"] += 1
            if self.declaration_enabled:
                event["business_dispatch"] = control["dispatched"]
            write_record(self.control_path, control)
            append_event(self.events_path, {**event, "phase": "dispatch"})
            started = time.monotonic()
            try:
                result = self.runtime.call(name, arguments)
                business_seconds = time.monotonic() - started
                json.dumps(result, allow_nan=False)
                validation_seconds = None
                if self.require_code_origins:
                    validation_started = time.monotonic()
                    try:
                        self.attest_sources("serve", verify_files=False)
                    except Exception:
                        control["source_validation_failed"] = True
                        raise
                    validation_seconds = time.monotonic() - validation_started
                append_event(self.events_path, {"sequence": sequence, "phase": "result",
                    "result": result, "elapsed_seconds": time.monotonic() - started,
                    "business_call_seconds": business_seconds,
                    "source_validation_seconds": validation_seconds})
            except Exception as error:
                control["execution_unknown"] = True
                write_record(self.control_path, control)
                append_event(self.events_path, {"sequence": sequence, "phase": "execution_unknown",
                                                "exception_type": type(error).__name__})
                raise GatewayError("EXECUTION_UNKNOWN", 503) from None
            control["inflight"] = None
            if self.declaration_enabled:
                control["declarations"]["last_resolved_business_dispatch"] = control["dispatched"]
            write_record(self.control_path, control)
            return {"sequence": sequence, "result": result}

    def declaration_report(self, control, business_events):
        state = self._declaration_state(control, fresh=True)
        try:
            if state is None:
                raise ValueError("Invalid declaration recording")
            declaration_events = [declarations.strict_json(line) for line in self.declarations_path.read_bytes().splitlines()]
            attempts = [e for e in business_events if e["phase"] in {"dispatch", "rejected"}]
            ordered = sorted([(e["event_index"], False, e) for e in attempts] +
                             [(e["event_index"], True, e) for e in declaration_events])
            cut = 0
            replay = declarations.DeclarationState(self.task["skill_ids"])
            for index, (event_index, is_declaration, event) in enumerate(ordered, 1):
                if type(event_index) is not int or event_index != index:
                    raise ValueError("Gateway event order mismatch")
                if is_declaration:
                    if event["last_resolved_business_dispatch"] != cut:
                        raise ValueError("Declaration cut differs from business order")
                    replay.accept(event)
                else:
                    attribution = event["attribution"]
                    if (not isinstance(attribution, dict)
                            or attribution != replay.attribution(attribution.get("skill_run_id", declarations.MISSING))):
                        raise ValueError("Business attribution differs from declaration order")
                    if event["phase"] == "dispatch":
                        cut += 1
                        if event["business_dispatch"] != cut:
                            raise ValueError("Business dispatch order mismatch")
            if (len(ordered) != control["declarations"]["event_index"] or cut != control["dispatched"]
                    or len(attempts) != control["attempts"]):
                raise ValueError("Gateway attempt count mismatch")
            report = {"schema": declarations.PROTOCOL, "valid": True, "claims": state.claims(),
                      "declaration_attempts": state.count, "unique_runs": len(state.runs),
                      "recorded_call_events": len(ordered), "received_requests": "unavailable",
                      "admission_rejections": control["admission_rejections"],
                      "uncounted_rejections": "HTTP parsing, sealed, execution unknown, declaration recording invalid",
                      "business_attempts": control["attempts"],
                      "business_dispatches": control["dispatched"], "claim_evaluation": "not_implemented"}
        except (KeyError, TypeError, ValueError, OSError, RecursionError):
            self._invalidate_declarations(control)
            report = {"schema": declarations.PROTOCOL, "valid": False, "claims": [],
                      "claim_evaluation": "not_implemented", "reason": "DECLARATION_RECORDING_INVALID"}
        try:
            write_record(self.state_root / "declaration_claims.json", report)
        except OSError:
            self._invalidate_declarations(control)
            report = {"schema": declarations.PROTOCOL, "valid": False, "claims": [],
                      "claim_evaluation": "not_implemented", "claims_file_valid": False,
                      "reason": "DECLARATION_REPORT_WRITE_FAILED"}
        return report

    def seal_and_score(self):
        with self.locked():
            # Publication is a one-shot operation, including after a partial failure.
            if any((self.state_root / name).exists() or (self.state_root / name).is_symlink()
                   for name in ("SEAL_STARTED.json", "evaluation.json", "declaration_claims.json")):
                raise GatewayError("SEAL_REUSE_FORBIDDEN")
            with (self.state_root / "SEAL_STARTED.json").open("x") as marker:
                json.dump({"phase": "started"}, marker)
                marker.flush()
                import os
                os.fsync(marker.fileno())
            control = load_json(self.control_path)
            control["sealed"] = True
            write_record(self.control_path, control)
            if control["execution_unknown"] or control["inflight"] is not None:
                raise GatewayError("EXECUTION_UNKNOWN", 503)
            from skillhorizon.experiment.sealed_fieldops_score import (
                evaluate_sealed_fieldops, validate_sealed_gateway_records)
            fieldops = (self.task["format"] == "skillhorizon.operations.v1"
                        and self.runtime.project.get("domain") == "fieldops")
            if fieldops:
                # The sealed-state evaluator checks these records once below.
                events = load_jsonl(self.events_path)
            else:
                events, _ = validate_sealed_gateway_records(self.state_root)
            from skillhorizon.experiment.environment_score import evaluate_environment
            if self.require_code_origins:
                self.attest_sources("verify", publish=False)
            from skillhorizon.experiment.internal_mcp import FORMAT
            if fieldops:
                score = evaluate_sealed_fieldops(self.task, self.task_root / "checkpoint", self.runtime_root)
            elif self.require_code_origins and self.task["format"] == FORMAT:
                from .score_worker import evaluate_attested
                score = evaluate_attested(self.task_root, self.state_root)
            else:
                score = evaluate_environment(self.task, self.task_root / "checkpoint", self.runtime_root)
            if self.require_code_origins:
                self.attest_sources("verify")
            report = {"adapter": "harbor_full_online_v1", "business": score,
                      "gateway": control, "provider_calls": None,
                      "skill_file_reads": "unavailable_from_business_log",
                      "direct_terminal_script_executions": "unavailable_from_business_log"}
            if self.declaration_enabled:
                report["declarations"] = self.declaration_report(control, events)
            write_record(self.state_root / "evaluation.json", report)
            if score.get("valid") is not True or type(score.get("task_success")) is not bool:
                raise ValueError("Invalid business evaluation; no business reward")
            return report


def make_server(gateway, address):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(30)

        def log_message(self, *_):
            pass

        def reply(self, status, body):
            payload = json.dumps(body, ensure_ascii=False, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # The trusted result was committed before delivery was attempted.

        def do_GET(self):
            if self.path == "/tools":
                self.reply(200, {"tools": list(gateway.tools.values())})
            elif self.path == "/health":
                self.reply(200, {"ready": True})
            else:
                self.reply(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if self.path != "/call":
                self.reply(404, {"error": "NOT_FOUND"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 2 * 1024 * 1024:
                    raise ValueError("body size")
                body = declarations.strict_json(self.rfile.read(size))
                json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
                if (not isinstance(body, dict) or not {"name", "arguments"} <= set(body)
                        or set(body) - {"name", "arguments", "skill_run_id"}
                        or ("skill_run_id" in body and not gateway.declaration_enabled)):
                    raise ValueError("body fields")
            except (ValueError, UnicodeError, RecursionError):
                self.reply(400, {"error": "INVALID_REQUEST"})
                return
            try:
                attribution = {"skill_run_id": body["skill_run_id"]} if "skill_run_id" in body else {}
                self.reply(200, gateway.call(body["name"], body["arguments"], **attribution))
            except GatewayError as error:
                self.reply(error.status, {"error": error.code})
            except Exception:
                self.reply(503, {"error": "GATEWAY_INFRA_FAILURE"})

    return ThreadingHTTPServer(address, Handler)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["serve", "verify"])
    parser.add_argument("task", type=Path)
    parser.add_argument("state", type=Path)
    parser.add_argument("--max-calls", type=int, required=True)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--require-code-origins", action="store_true")
    args = parser.parse_args()
    if args.action == "serve":
        args.state.mkdir(parents=True, exist_ok=True)
        with (args.state / "server.lock").open("a") as owner:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            gateway = BusinessGateway(args.task, args.state, max_calls=args.max_calls,
                                      require_code_origins=args.require_code_origins)
            if args.require_code_origins:
                gateway.attest_sources("serve")
            server = make_server(gateway, (args.host, args.port))
            import os
            write_record(args.state / "SERVER.json", {"host": args.host, "port": server.server_port,
                                                       "pid": os.getpid()})
            try:
                server.serve_forever()
            finally:
                server.server_close()
    else:
        gateway = BusinessGateway(args.task, args.state, max_calls=args.max_calls, create=False,
                                  require_code_origins=args.require_code_origins)
        report = gateway.seal_and_score()
        print(json.dumps({"valid": report["business"]["valid"], "task_success": report["business"]["task_success"]}))


if __name__ == "__main__":
    main()
