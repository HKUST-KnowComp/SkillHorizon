"""One serialized business authority; private Unix control socket for the host."""
import argparse
from copy import deepcopy
import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
import socketserver
import threading
from urllib.parse import unquote

from jsonschema import Draft202012Validator

from .common import decode, read, write
from .gateways import accepted, create

DECLARATIONS = frozenset({"begin_skill", "finish_skill"})
MAX_BODY = 2 * 1024 * 1024


class BusinessService:
    def __init__(self, root, B, gateway_factory):
        if type(B) is not int or B <= 0:
            raise ValueError("Positive integer B required")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.closing = threading.Event()
        self.owner = (self.root / "owner.lock").open("a+")
        try:
            fcntl.flock(self.owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if (self.root / "STATE.json").exists():
                raise ValueError("Existing service state is not automatically resumable")
            self.state = dict(version=1, B=B, attempts=0, events=0, inflight=None,
                              phase="initializing", uncertain=False, sealed=False, closing=False)
            self._persist()
            self.gateway, profile = gateway_factory()
            tools = self.gateway.tools()["tools"]
            self.tools = {t["name"]: t for t in tools if t["name"] in profile}
            self.validators = {name: Draft202012Validator(t["parameters"])
                               for name, t in self.tools.items()}
            for tool in self.tools.values():
                Draft202012Validator.check_schema(tool["parameters"])
            self.state["phase"] = "ready"
            self._persist()
        except BaseException:
            self.owner.close()
            raise

    def _persist(self):
        write(self.root / "STATE.json", self.state)
        self._publish_status()

    def _publish_status(self):
        self._status = {k: self.state[k] for k in
                        ("phase", "B", "attempts", "uncertain", "sealed", "inflight")}

    def status(self):
        # Replace snapshots atomically; polling must not wait for business work.
        return {**self._status, "closing": self.closing.is_set()}

    def _uncertain(self):
        self.state.update(phase="uncertain", uncertain=True)
        self._publish_status()
        try:
            self._persist()
        except OSError:
            pass  # The last durable inflight record also forbids restart/replay.

    def attempt(self, body):
        if self.closing.is_set():
            status = self.status()
            reason = "CLOSING" if status["phase"] == "ready" else status["phase"].upper()
            return 409, {"ok": False, "error": reason, **status}
        with self.lock:
            if self.closing.is_set() or self.state["phase"] != "ready":
                reason = "CLOSING" if self.state["phase"] == "ready" else self.state["phase"].upper()
                return 409, {"ok": False, "error": reason, **self.status()}
            name = body.get("name") if isinstance(body, dict) else None
            declaration = isinstance(name, str) and name in DECLARATIONS
            self.state["events"] += 1
            if not declaration:
                self.state["attempts"] += 1
            event = self.state["events"]
            self.state["inflight"] = event
            try:
                # Admission is durable before schema checks or any runtime entry.
                self._persist()
                arguments = body.get("arguments") if isinstance(body, dict) else None
                if isinstance(body, dict) and isinstance(body.get("arguments_json"), str):
                    try:
                        arguments = decode(body["arguments_json"])
                    except (ValueError, RecursionError):
                        arguments = None
                invalid = (not isinstance(body, dict)
                           or set(body) - {"name", "arguments", "arguments_json", "skill_run_id"}
                           or "name" not in body
                           or ("arguments" in body) == ("arguments_json" in body)
                           or ("arguments_json" in body and not isinstance(body["arguments_json"], str))
                           or not isinstance(name, str) or name not in self.tools
                           or not isinstance(arguments, dict)
                           or ("skill_run_id" in body and not isinstance(body["skill_run_id"], str)))
                if invalid:
                    result = {"ok": False, "error": {"code": "INVALID_OR_UNKNOWN_TOOL"}}
                elif not self.validators[name].is_valid(arguments):
                    result = {"ok": False, "error": {"code": "PUBLIC_SCHEMA"}}
                else:
                    metadata = {"skill_run_id": body["skill_run_id"]} if "skill_run_id" in body else {}
                    result = self.gateway.call(name, arguments, **metadata)
                json.dumps(result, allow_nan=False)
                write(self.root / ("event-%08d.json" % event),
                      {"request": body, "result": result, "charged": not declaration,
                       "attempts": self.state["attempts"]})
                self.state["inflight"] = None
                if self.state["attempts"] >= self.state["B"]:
                    self.state["phase"] = "exhausted"
                self._persist()
            except Exception:
                self._uncertain()
                return 503, {"ok": False, "error": "EXECUTION_UNCERTAIN", **self.status()}
            return 200, {"result": result, "charged": not declaration, **self.status()}

    def freeze(self):
        # Close admission before waiting for the current worker. Its durable
        # inflight record already prevents recovery if this process dies here.
        self.closing.set()
        with self.lock:
            self.state["closing"] = True
            if self.state["uncertain"] or self.state["inflight"] is not None:
                self._persist()
                raise ValueError("Unsettled execution cannot be frozen for scoring")
            if self.state["phase"] in {"ready", "exhausted"}:
                self.state["phase"] = "frozen"
            elif self.state["phase"] not in {"frozen", "sealed", "scored"}:
                raise ValueError("Service cannot be frozen")
            try:
                self._persist()
            except Exception:
                self._uncertain()
                raise
            return self.status()

    def seal(self):
        self.freeze()
        with self.lock:
            if self.state["phase"] == "scored":
                return read(self.root / "EVALUATION.json")
            self.state["sealed"] = True
            if self.state["uncertain"] or self.state["inflight"] is not None:
                self._persist()
                raise ValueError("Unsettled execution has no authoritative score")
            if self.state["phase"] != "frozen":
                raise ValueError("Service cannot be scored")
            endpoint = "B_exhausted" if self.state["attempts"] >= self.state["B"] else "native_agent_end"
            self.state["phase"] = "sealed"
            self._persist()
            score = self.gateway.seal_and_score()
            report = {"profile": "native_external_v1", "business": score,
                      "accepted": accepted(score), "endpoint": endpoint, "state": deepcopy(self.state)}
            write(self.root / "EVALUATION.json", report)
            self.state["phase"] = "scored"
            self._persist()
            return report

    def close(self):
        try:
            self.gateway.close()
        finally:
            self.owner.close()


def http_server(service, address):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, code, value):
            payload = json.dumps(value, allow_nan=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            try:
                self.wfile.write(payload)
            except (BrokenPipeError, ConnectionResetError):
                pass  # Delivery failure must never roll back or replay a write.

        def do_GET(self):
            if self.path == "/health":
                self.reply(200, {"ready": True})
            elif self.path == "/tools":
                self.reply(200, {"tools": list(service.tools.values())})
            elif self.path.startswith("/schema/"):
                tool = service.tools.get(unquote(self.path[len("/schema/"):]))
                self.reply(200 if tool else 404, tool or {"error": "UNKNOWN_TOOL"})
            else:
                self.reply(404, {"error": "NOT_FOUND"})

        def do_POST(self):
            if self.path != "/call":
                self.reply(404, {"error": "NOT_FOUND"})
                return
            try:
                self.connection.settimeout(30)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_BODY or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid framing")
                body = decode(self.rfile.read(size))
            except (ValueError, UnicodeError, OSError, RecursionError):
                body = None
            self.reply(*service.attempt(body))

    return ThreadingHTTPServer(address, Handler)


def control_server(service, path):
    class Handler(socketserver.StreamRequestHandler):
        def handle(self):
            operation = self.rfile.readline(80).strip()
            try:
                operations = {b"seal": service.seal, b"freeze": service.freeze, b"status": service.status}
                value = operations[operation]() if operation in operations else None
                if value is None:
                    raise ValueError("Unknown control command")
            except Exception:
                value = {"error": "PRIVATE_CONTROL_FAILED", "state": service.status()}
            self.wfile.write(json.dumps(value).encode() + b"\n")
    server = socketserver.ThreadingUnixStreamServer(str(path), Handler)
    Path(path).chmod(0o600)
    return server


def control(path, operation):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(600)
        connection.connect(str(path))
        connection.sendall(operation.encode() + b"\n")
        with connection.makefile("rb") as stream:
            return decode(stream.readline())


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--input", type=Path, default=Path("/input"))
    serve.add_argument("--state", type=Path, default=Path("/state"))
    for command in ("status", "freeze", "seal"):
        ctl = sub.add_parser(command)
        ctl.add_argument("--socket", type=Path, default=Path("/state/control.sock"))
    args = parser.parse_args(argv)
    if args.command != "serve":
        result = control(args.socket, args.command)
        print(json.dumps(result))
        return 1 if "error" in result else 0
    config = read(args.input / "service.json")
    service = BusinessService(args.state, config["B"], lambda: create(
        config["environment"], args.input / "task", args.state / "business",
        config["B"], args.input / "public.json"))
    private = control_server(service, args.state / "control.sock")
    public = http_server(service, ("0.0.0.0", 8765))
    thread = threading.Thread(target=private.serve_forever, daemon=True)
    thread.start()
    try:
        public.serve_forever()
    finally:
        public.server_close()
        private.shutdown()
        private.server_close()
        thread.join()
        service.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
