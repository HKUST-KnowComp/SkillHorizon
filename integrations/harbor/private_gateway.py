"""Run and verify only the exported private business code, in child processes."""

import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import time

from .client import Client, ClientError


class PrivateGatewayError(RuntimeError):
    """Fixed diagnostic labels only; subprocess output can contain private data."""

    LABELS = {"PRIVATE_RECORD_MISSING_OR_INVALID", "PRIVATE_GATEWAY_ARGUMENTS_INVALID",
              "PRIVATE_SOURCE_MANIFEST_INVALID", "PRIVATE_STATE_REUSE_FORBIDDEN",
              "PRIVATE_SERVE_ORIGIN_MISMATCH", "PRIVATE_VERIFY_ORIGIN_MISMATCH",
              "PRIVATE_EVALUATOR_ORIGIN_MISMATCH", "PRIVATE_SERVE_EXITED",
              "PRIVATE_SERVER_IDENTITY_MISMATCH", "PRIVATE_HEALTH_INVALID", "PRIVATE_SERVE_TIMEOUT",
              "PRIVATE_VERIFY_REUSE_FORBIDDEN", "PRIVATE_VERIFY_TIMEOUT", "PRIVATE_VERIFY_FAILED",
              "PRIVATE_SOURCE_MANIFEST_CHANGED", "PRIVATE_EVALUATION_INVALID",
              "PRIVATE_EVALUATOR_SCORE_MISMATCH", "PRIVATE_SOURCE_ROOT_MISMATCH",
              "PRIVATE_PROCESS_REAP_FAILED"}

    def __init__(self, code):
        super().__init__(code if code in self.LABELS else "PRIVATE_GATEWAY_FAILED")


def read_record(path):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise PrivateGatewayError("PRIVATE_RECORD_MISSING_OR_INVALID")
    try:
        value = json.loads(path.read_text(encoding="utf-8"),
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (OSError, ValueError):
        raise PrivateGatewayError("PRIVATE_RECORD_MISSING_OR_INVALID") from None
    if not isinstance(value, dict):
        raise PrivateGatewayError("PRIVATE_RECORD_MISSING_OR_INVALID")
    return value


def score_digest(score):
    return hashlib.sha256(json.dumps(score, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def private_environment(code):
    env = {key: os.environ[key] for key in ("PATH", "HOME", "TIKTOKEN_CACHE_DIR") if key in os.environ}
    code = Path(code).resolve()
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONPATH=os.pathsep.join((str(code / "src"), str(code))))
    return env


def reap(process):
    if process is None:
        return
    if process.poll() is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            raise PrivateGatewayError("PRIVATE_PROCESS_REAP_FAILED") from None


class PrivateGateway:
    def __init__(self, business_root, state_root, *, max_calls, startup_timeout=30, verify_timeout=550,
                 continuation_source=None):
        self.business_root = Path(business_root).resolve()
        self.code = self.business_root / "code"
        self.task = self.business_root / "task"
        self.state = Path(state_root).resolve()
        self.process = self.verifier = None
        self.url = None
        self.control_probes = 0
        self._score_started = False
        if (type(max_calls) is not int or max_calls < 1
                or self.state.is_relative_to(self.business_root)
                or self.business_root.is_relative_to(self.state)):
            raise PrivateGatewayError("PRIVATE_GATEWAY_ARGUMENTS_INVALID")
        self.max_calls = max_calls
        self.verify_timeout = verify_timeout
        self.manifest_path = self.code / "CODE_ORIGINS.json"
        self.manifest = read_record(self.manifest_path)
        if (self.manifest.get("version") != 1 or not self.manifest.get("files")
                or any(not isinstance(self.manifest.get(key), str)
                       or not Path(self.manifest[key]).is_absolute()
                       for key in ("business_source_root", "adapter_source_root"))):
            raise PrivateGatewayError("PRIVATE_SOURCE_MANIFEST_INVALID")
        self.manifest_digest = hashlib.sha256(self.manifest_path.read_bytes()).hexdigest()
        self.business_source_root = self.manifest["business_source_root"]
        task = read_record(self.task / "task.json")
        self.internal_domain = (task.get("format") == "skillhorizon.internal_mcp_task.v1"
                                or task.get("config", {}).get("environment_id") in
                                {"C01", "C06", "C09", "C11", "C25", "C47", "C50"})
        if self.state.exists() or Path(state_root).is_symlink():
            raise PrivateGatewayError("PRIVATE_STATE_REUSE_FORBIDDEN")
        self.state.mkdir(parents=True, exist_ok=False)
        if continuation_source is not None:
            source = Path(continuation_source).resolve()
            control = read_record(source / "control.json")
            if (control.get("sealed") is not True or control.get("inflight") is not None
                    or control.get("execution_unknown") is not False
                    or control.get("declarations", {}).get("pending")
                    or control.get("declarations", {}).get("invalid")):
                raise PrivateGatewayError("PRIVATE_STATE_REUSE_FORBIDDEN")
            shutil.copytree(source / "runtime", self.state / "runtime")
            for name in ("gateway.jsonl", "declarations.jsonl"):
                if (source / name).exists():
                    shutil.copyfile(source / name, self.state / name)
            control.update(sealed=False, max_calls=max_calls)
            (self.state / "control.json").write_text(json.dumps(control) + "\n")
        self.environment = private_environment(self.code)
        try:
            self.process = self._launch("serve")
            self._wait_ready(startup_timeout)
        except BaseException:
            self.close()
            raise

    def _launch(self, action):
        command = [sys.executable, "-B", "-m", "integrations.harbor.backend", action,
                   str(self.task), str(self.state), "--max-calls", str(self.max_calls),
                   "--require-code-origins"]
        if action == "serve":
            command.extend(["--host", "127.0.0.1", "--port", "0"])
        # Inherit the cell process group: the outer 600s controller owns all descendants.
        log = self.state / (action + "-process.log")
        descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            return subprocess.Popen(command, cwd=self.code, env=self.environment,
                                    stdin=subprocess.DEVNULL, stdout=stream,
                                    stderr=subprocess.STDOUT, start_new_session=False)

    def _receipt(self, phase):
        label = "PRIVATE_" + phase.upper() + "_ORIGIN_MISMATCH"
        try:
            receipt = read_record(self.state / ("CODE_ORIGINS_" + phase.upper() + ".json"))
        except PrivateGatewayError:
            raise PrivateGatewayError(label) from None
        if (receipt.get("version") != 1 or receipt.get("status") != "passed"
                or receipt.get("phase") != ("serve" if phase == "serve" else "verify")
                or receipt.get("code_manifest_sha256") != self.manifest_digest
                or any(receipt.get(key) != self.manifest[key]
                       for key in ("business_source_root", "adapter_source_root"))
                or not isinstance(receipt.get("modules"), dict) or not receipt["modules"]):
            raise PrivateGatewayError(label)
        return receipt

    def _wait_ready(self, timeout):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise PrivateGatewayError("PRIVATE_SERVE_EXITED")
            metadata = self.state / "SERVER.json"
            if metadata.exists() or metadata.is_symlink():
                server = read_record(metadata)
                if (server.get("host") != "127.0.0.1" or type(server.get("port")) is not int
                        or not 1 <= server["port"] <= 65535 or type(server.get("pid")) is not int
                        or server["pid"] != self.process.pid):
                    raise PrivateGatewayError("PRIVATE_SERVER_IDENTITY_MISMATCH")
                self._receipt("serve")
                url = "http://127.0.0.1:" + str(server["port"])
                try:
                    self.control_probes += 1
                    ready = Client(url, timeout=1)._request("/health")
                except ClientError:
                    pass
                else:
                    if ready != {"ready": True}:
                        raise PrivateGatewayError("PRIVATE_HEALTH_INVALID")
                    if self.process.poll() is not None:
                        raise PrivateGatewayError("PRIVATE_SERVE_EXITED")
                    self.url = url
                    return
            time.sleep(0.02)
        raise PrivateGatewayError("PRIVATE_SERVE_TIMEOUT")

    def seal_and_score(self):
        if self._score_started:
            raise PrivateGatewayError("PRIVATE_VERIFY_REUSE_FORBIDDEN")
        self._score_started = True
        if self.process is None or self.process.poll() is not None:
            raise PrivateGatewayError("PRIVATE_SERVE_EXITED")
        for filename in ("evaluation.json", "CODE_ORIGINS_VERIFY.json", "CODE_ORIGINS_EVALUATOR.json"):
            path = self.state / filename
            if path.exists() or path.is_symlink():
                raise PrivateGatewayError("PRIVATE_VERIFY_REUSE_FORBIDDEN")
        self._receipt("serve")
        try:
            self.verifier = self._launch("verify")
            try:
                returncode = self.verifier.wait(timeout=self.verify_timeout)
            except subprocess.TimeoutExpired:
                raise PrivateGatewayError("PRIVATE_VERIFY_TIMEOUT") from None
            if returncode != 0:
                raise PrivateGatewayError("PRIVATE_VERIFY_FAILED")
        finally:
            reap(self.verifier)
        return self._verified_report()

    def _verified_report(self):
        if self.process.poll() is not None:
            raise PrivateGatewayError("PRIVATE_SERVE_EXITED")
        if hashlib.sha256(self.manifest_path.read_bytes()).hexdigest() != self.manifest_digest:
            raise PrivateGatewayError("PRIVATE_SOURCE_MANIFEST_CHANGED")
        self._receipt("serve")
        self._receipt("verify")
        report = read_record(self.state / "evaluation.json")
        business, control = report.get("business"), report.get("gateway")
        if (not isinstance(business, dict) or business.get("valid") is not True
                or type(business.get("task_success")) is not bool or not isinstance(control, dict)
                or control.get("sealed") is not True or control.get("execution_unknown") is not False
                or control.get("inflight") is not None or control.get("max_calls") != self.max_calls
                or control != read_record(self.state / "control.json")):
            raise PrivateGatewayError("PRIVATE_EVALUATION_INVALID")
        if self.internal_domain:
            evaluator = self._receipt("evaluator")
            if (evaluator.get("role") != "internal_domain_evaluator"
                    or evaluator.get("score_digest") != score_digest(business)):
                raise PrivateGatewayError("PRIVATE_EVALUATOR_SCORE_MISMATCH")
        return report

    def measure_claims(self, *, timeout=120):
        """Explicit post-score phase with its own timeout, outside verify_timeout."""
        from .claim_measurement import measure_claims
        try:
            if type(timeout) not in (int, float) or not 0 < timeout <= 300:
                raise ValueError("Claims timeout must be finite and within300 seconds")
            self._verified_report()
            return measure_claims(self.code, self.task, self.state, require_code_origins=True, timeout=timeout)
        except Exception as error:
            return {"status": "invalid", "failure_type": type(error).__name__}

    def close(self):
        try:
            reap(self.verifier)
        finally:
            reap(self.process)
