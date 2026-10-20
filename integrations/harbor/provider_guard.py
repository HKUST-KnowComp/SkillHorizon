"""Controller-only, fail-closed Stage D Chat Completions budget proxy.

Runner API: BudgetProxy(state_dir, model=..., upstream_url=..., api_key=...),
then make_server(proxy), serve_forever in a controller thread, and point Harbor
at http://127.0.0.1:<server_port>/v1 with any dummy API key. snapshot() returns
the durable ledger. No request/response content or credentials enter the ledger.
The constructor permits numeric loopback HTTP upstreams with testing_only=True;
the default and CLI permit only the fixed OpenLux production endpoint.
"""

import argparse
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import fcntl
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import math
import multiprocessing
import os
from pathlib import Path
from types import SimpleNamespace
import queue
import re
import ssl
import tempfile
import threading
import time
from typing import NamedTuple
import urllib.error
import urllib.parse
import urllib.request

import tiktoken


PRODUCTION_URL = "https://api.openlux.ai/v1/chat/completions"
MAX_BODY = 16 * 1024 * 1024
LIMITS = {"attempts": 32, "input": 300000, "output": 65536, "per_output": 4096}
MAX_DIAGNOSTIC_INTEGER = 2 ** 53 - 1


class TransportResult(NamedTuple):
    status: int
    body: bytes
    diagnostic: dict | None = None
    retry_after_seconds: int | None = None
    retry_after_error: str | None = None
    sent: bool | None = True
    send_started_at: float | None = None
    cooldown_notified: bool = False


class RequestStarted(NamedTuple):
    time: float


class RequestWritten(NamedTuple):
    time: float


class ResponseHead(NamedTuple):
    """Nonfinal IPC event; never a response or an accounting settlement."""
    status: int
    retry_after_seconds: int | None = None
    retry_after_error: str | None = None


def _retry_after(headers):
    """Normalize only Retry-After; never retain the header text."""
    if headers is None:
        return None, None
    values = headers.get_all("Retry-After", []) if hasattr(headers, "get_all") else (
        [headers["Retry-After"]] if "Retry-After" in headers else [])
    if not values:
        return None, None
    if len(values) != 1 or not isinstance(values[0], str):
        return None, "invalid_retry_after"
    value = values[0].strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            digits = value.lstrip("0") or "0"
            if len(digits) > 9:
                return None, "retry_after_exceeds_limit"
            seconds = int(digits)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None, "invalid_retry_after"
            seconds = max(0, math.ceil((date - datetime.now(timezone.utc)).total_seconds()))
        return seconds, "retry_after_exceeds_limit" if seconds > 60 else None
    except (ValueError, TypeError, OverflowError):
        return None, "invalid_retry_after"


class DiagnosticError(ValueError):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        super().__init__(diagnostic["code"])


class _DuplicateJSON(ValueError):
    pass


class _NonfiniteJSON(ValueError):
    pass


def _diagnostic(layer, code, **fields):
    # Callers supply fixed labels only, never provider keys or exception text.
    return {"layer": layer, "code": code, **fields}


def _type_label(value):
    return {type(None): "null", bool: "boolean", int: "integer", float: "number",
            str: "string", list: "array", dict: "object"}.get(type(value), "other")


class GuardError(Exception):
    """Only fixed, non-secret error codes are exposed to the caller."""

    def __init__(self, code, status=400):
        self.code, self.status = code, status
        super().__init__(code)


def _integer(value):
    return type(value) is int and value >= 0


def _pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJSON("duplicate")
        result[key] = value
    return result


def _invalid_constant(_):
    raise _NonfiniteJSON("nonfinite")


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise _NonfiniteJSON("nonfinite")
    return number


def _decode(raw):
    value = json.loads(raw, object_pairs_hook=_pairs, parse_constant=_invalid_constant, parse_float=_finite_float)
    _encode(value)  # Also rejects finite-looking literals such as 1e999.
    return value


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()


def _decode_response(raw):
    if not raw.strip():
        raise DiagnosticError(_diagnostic("decode", "empty_body"))
    try:
        return _decode(raw.decode("utf-8-sig"))
    except UnicodeError:
        code = "invalid_encoding"
    except _DuplicateJSON:
        code = "duplicate_key"
    except _NonfiniteJSON:
        code = "nonfinite_number"
    except (ValueError, RecursionError):
        code = "invalid_json"
    raise DiagnosticError(_diagnostic("decode", code)) from None


def _write(path, value):
    fd, name = tempfile.mkstemp(prefix=".ledger-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(_encode(value))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def _non200_details(response, secret=""):
    """Preserve bounded redacted error fields, never the raw provider body."""
    code, usage, safe = "non_200_status", None, {}
    try:
        raw = response.read(65537)
        if len(raw) <= 65536:
            try:
                decoded = _decode_response(raw)
            except DiagnosticError:
                decoded = None
            error = decoded.get("error", decoded) if isinstance(decoded, dict) else None
            if isinstance(error, str): error = {"message": error}
            if isinstance(error, dict):
                if any(error.get(field) == "context_length_exceeded" for field in ("code", "type")):
                    code = "context_length_exceeded"
                for field in ("code", "type", "message"):
                    value = error.get(field)
                    if isinstance(value, (str, int)):
                        value = str(value)
                        if secret: value = value.replace(secret, "[redacted]")
                        value = re.sub(r"(?i)(?:sk-|bearer\s+)[A-Za-z0-9_.\-]+", "[redacted]", value)
                        value = re.sub(r"[A-Za-z0-9_\-]{28,}", "[redacted-id]", value)
                        safe[field] = value[:600]
            elif raw:
                safe = {"type": "non_json_error_body", "message": "Provider returned a non-JSON HTTP error; raw body not retained."}
            try: usage = _usage(decoded)
            except (DiagnosticError, TypeError, AttributeError): pass
    except Exception:
        pass
    body = {}
    if usage is not None: body["usage"] = usage
    if safe: body["error"] = safe
    return code, _encode(body) if body else b""


def _non200_code(response):
    return _non200_details(response)[0]


def _send_worker(pipe, url, key, payload, timeout, traffic_spec=None, stop_path=None):
    """One send, no redirects, retries or ambient proxy settings."""
    status, phase = 0, "connection"
    retry_after, retry_error = None, None
    sent = False
    request_written = False

    def failure(code, body=b"", **details):
        pipe.send(TransportResult(status, body, _diagnostic("transport", code, **details), retry_after, retry_error, sent))

    try:
        handlers = [urllib.request.ProxyHandler({}), _NoRedirect()]
        if traffic_spec is not None:
            class GatedRequest:
                def check_stop(self):
                    if ((stop_path is not None and Path(stop_path).exists())
                            or (Path(traffic_spec["root"]) / "STOP").exists()):
                        raise GuardError("CONTROLLER_STOPPED", 503)

                def send(self, data):
                    self.check_stop()
                    if self.sock is None:
                        if self.auto_open:
                            self.connect()
                        else:
                            raise http.client.NotConnected()
                    self.check_stop()
                    return super().send(data)

                def request(self, *args, **kwargs):
                    nonlocal sent, request_written
                    self.check_stop()
                    sent = True
                    pipe.send(RequestStarted(time.time()))
                    result = super().request(*args, **kwargs)
                    request_written = True
                    pipe.send(RequestWritten(time.time()))
                    return result
            class HTTPConnection(GatedRequest, http.client.HTTPConnection):
                pass
            class HTTPSConnection(GatedRequest, http.client.HTTPSConnection):
                pass
            class HTTPHandler(urllib.request.HTTPHandler):
                def http_open(self, req):
                    return self.do_open(HTTPConnection, req)
            class HTTPSHandler(urllib.request.HTTPSHandler):
                def https_open(self, req):
                    return self.do_open(HTTPSConnection, req, context=self._context)
            handlers += [HTTPHandler(), HTTPSHandler()]
        opener = urllib.request.build_opener(*handlers)
        request = urllib.request.Request(url, payload, method="POST", headers={
            "Authorization": "Bearer " + key, "Content-Type": "application/json"})
        try:
            if traffic_spec is None:
                sent = True
            response = opener.open(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            status = error.code
            retry_after, retry_error = _retry_after(error.headers)
            pipe.send(ResponseHead(status, retry_after, retry_error))
            code, body = _non200_details(error, key)
            error.close()
            failure(code, body)
            return
        with response:
            status = response.status
            if status != 200:
                retry_after, retry_error = _retry_after(response.headers)
            pipe.send(ResponseHead(status, retry_after, retry_error))
            if status != 200:
                failure(*_non200_details(response, key))
                return
            lengths = response.headers.get_all("Content-Length", [])
            if lengths and (len(lengths) != 1 or re.fullmatch(r"[0-9]+", lengths[0]) is None):
                failure("invalid_length_header")
                return
            declared = None
            if lengths:
                digits = lengths[0].lstrip("0") or "0"
                if len(digits) > len(str(MAX_BODY)) or int(digits) > MAX_BODY:
                    failure("body_limit_exceeded")
                    return
                declared = int(digits)
            phase = "read"
            raw = response.read(MAX_BODY + 1)
            if len(raw) > MAX_BODY:
                failure("body_limit_exceeded")
            elif declared is not None and len(raw) != declared:
                failure("length_mismatch")
            else:
                pipe.send(TransportResult(status, raw))
    except Exception as error:
        # Exception strings may include URLs, request data or credentials.
        reason = error.reason if isinstance(error, urllib.error.URLError) else error
        if isinstance(reason, GuardError) and reason.code == "CONTROLLER_STOPPED":
            code = "controller_stopped"
        elif isinstance(reason, ssl.SSLCertVerificationError):
            code = "certificate_error"
        elif isinstance(reason, TimeoutError):
            code = "timeout"
        elif isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError,
                                 http.client.RemoteDisconnected, http.client.IncompleteRead, ssl.SSLEOFError)):
            code = "disconnected"
        elif isinstance(reason, (ssl.SSLError, ValueError)):
            code = "configuration_error"
        else:
            code = "read_error" if phase == "read" else "connection_error"
        try:
            details = {"exception_type": type(reason).__name__, "phase": phase,
                       "request_written": request_written}
            if type(getattr(reason, "errno", None)) is int:
                details["errno"] = reason.errno
            failure(code, **details)
        except (OSError, EOFError):
            pass
    finally:
        pipe.close()


def _send(url, key, payload, timeout, stop_path=None, traffic_spec=None, logical_deadline=None,
          execution_clock=None, request_clock=None, on_admitted=None):
    if request_clock is not None:
        return _send_queue_aware(url, key, payload, timeout, stop_path, traffic_spec,
                                 execution_clock, request_clock, on_admitted)
    if execution_clock is not None:
        return _send_timed(url, key, payload, timeout, stop_path, traffic_spec,
                           logical_deadline, execution_clock)
    if traffic_spec:
        from .pilot_traffic import SharedTraffic
        deadline = logical_deadline if logical_deadline is not None else time.monotonic() + timeout
        entered_transport = False
        stopped = lambda: ((stop_path is not None and Path(stop_path).exists())
                           or (Path(traffic_spec["root"]) / "STOP").exists())
        try:
            traffic = SharedTraffic(**traffic_spec, operation_deadline=deadline, stopped=stopped)
            with traffic.admitted(deadline, stopped) as token:
                remaining = min(timeout, deadline - time.monotonic())
                if remaining <= 0:
                    return TransportResult(0, b"", _diagnostic("transport", "queue_deadline"), sent=False)
                entered_transport = True
                result = _send_impl(url, key, payload, remaining, stop_path, traffic_spec, admission_token=token)
                try:
                    traffic.completed(result.status, diagnostic=result.diagnostic)
                except GuardError as error:
                    _write(traffic.root / "HEALTH_ALERT.json", dict(action="completion_accounting_deferred",
                        code=error.code, time=time.time()))
                except Exception as error:
                    traffic.stop("traffic_completion_state_error", error_type=type(error).__name__)
                return result
        except GuardError as error:
            code = "controller_stopped" if error.code == "CONTROLLER_STOPPED" else "queue_deadline"
            return TransportResult(0, b"", _diagnostic("transport", code), sent=None if entered_transport else False)
    return _send_impl(url, key, payload, timeout, stop_path, traffic_spec)


def _send_queue_aware(url, key, payload, timeout, stop_path, traffic_spec,
                      clock, request_clock, on_admitted):
    """Direct-call contract: stable supervised queue, then bounded physical send."""
    from contextlib import nullcontext
    from .execution_clock import infrastructure_failure
    from .pilot_traffic import SharedTraffic
    from .request_clock import MODE, HEARTBEAT_GRACE
    stopped = lambda: ((stop_path is not None and Path(stop_path).exists())
        or bool(traffic_spec and (Path(traffic_spec['root']) / 'STOP').exists()))
    entered_transport = False
    traffic, token = None, None
    try:
        with ExitStack() as leases:
            if traffic_spec:
                queue_scope = (clock.excluded_phase('scheduler_queue',
                    guard_deadline=time.monotonic()+HEARTBEAT_GRACE) if clock else nullcontext())
                with queue_scope, request_clock.queued(clock) as heartbeat:
                    # Constructor and lock failures are definitely pre-send.
                    traffic = SharedTraffic(**traffic_spec, stopped=stopped, read_existing=True)
                    token = leases.enter_context(traffic.admitted(None, stopped,
                        queue_wait_mode=MODE, heartbeat=heartbeat))
            available = min(timeout, request_clock.remaining())
            remaining = clock.remaining() if clock else float('inf')
            if stopped():
                return TransportResult(0, b'', _diagnostic('transport', 'controller_stopped'), sent=False)
            if available <= 0 or remaining <= 0:
                return TransportResult(0, b'', _diagnostic('transport', 'logical_deadline'), sent=False)
            if on_admitted is None:
                raise GuardError('SEND_RESERVATION_REQUIRED', 503)
            on_admitted()
            # Reservation I/O is active request time too.
            available = min(available, request_clock.remaining())
            remaining = clock.remaining() if clock else float('inf')
            if stopped() or available <= 0 or remaining <= 0:
                code = 'controller_stopped' if stopped() else 'logical_deadline'
                return TransportResult(0, b'', _diagnostic('transport', code), sent=False)
            scope = (clock.excluded_phase('provider_pending',
                guard_deadline=request_clock.deadline()+10) if clock else nullcontext({}))
            with scope as phase:
                phase['charge'] = True
                entered_transport = True
                result = _send_impl(url, key, payload, min(available, remaining), stop_path,
                                    traffic_spec, admission_token=token)
                phase['charge'] = not infrastructure_failure(result, budget_clipped=remaining <= available)
                phase['reason'] = 'api_failure'
            if traffic is not None:
                try:
                    traffic.completed(result.status, diagnostic=result.diagnostic)
                except GuardError as error:
                    _write(traffic.root / 'HEALTH_ALERT.json', dict(action='completion_accounting_deferred',
                        code=error.code, time=time.time()))
            return result
    except GuardError as error:
        code = 'controller_stopped' if error.code == 'CONTROLLER_STOPPED' else 'queue_lock_error'
        return TransportResult(0, b'', _diagnostic('transport', code, guard_code=error.code),
                               sent=None if entered_transport else False)


def _send_timed(url, key, payload, timeout, stop_path, traffic_spec, logical_deadline, clock):
    from .execution_clock import infrastructure_failure
    from .pilot_traffic import SharedTraffic
    deadline = logical_deadline if logical_deadline is not None else time.monotonic() + timeout
    stopped = lambda: ((stop_path is not None and Path(stop_path).exists())
        or bool(traffic_spec and (Path(traffic_spec['root']) / 'STOP').exists()))
    traffic, token = None, None
    with ExitStack() as stack:
        if traffic_spec:
            try:
                with clock.excluded_phase('scheduler_queue', guard_deadline=deadline + 10):
                    traffic = SharedTraffic(**traffic_spec, operation_deadline=deadline, stopped=stopped)
                    token = stack.enter_context(traffic.admitted(deadline, stopped))
            except GuardError as error:
                code = 'controller_stopped' if error.code == 'CONTROLLER_STOPPED' else 'queue_deadline'
                return TransportResult(0, b'', _diagnostic('transport', code), sent=False)
        available = min(timeout, max(0, deadline-time.monotonic()))
        remaining = clock.remaining()
        if remaining <= 0 or available <= 0:
            return TransportResult(0, b'', _diagnostic('transport', 'queue_deadline'), sent=False)
        budget_clipped = remaining <= available
        with clock.excluded_phase('provider_pending', guard_deadline=deadline + 10) as phase:
            # Unknown exceptions are charged until a definite transport result exists.
            phase['charge'] = True
            result = _send_impl(url, key, payload, min(available, remaining), stop_path,
                                traffic_spec, admission_token=token)
            phase['charge'] = not infrastructure_failure(result, budget_clipped=budget_clipped)
            phase['reason'] = 'api_failure'
        if traffic is not None:
            with clock.excluded_phase('scheduler_accounting', guard_deadline=deadline + 10):
                try:
                    traffic.completed(result.status, diagnostic=result.diagnostic)
                except GuardError as error:
                    _write(traffic.root / 'HEALTH_ALERT.json', dict(action='completion_accounting_deferred',
                        code=error.code, time=time.time()))
                finally:
                    stack.close()
        return result


def _send_impl(url, key, payload, timeout, stop_path=None, traffic_spec=None, admission_token=None):
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    arguments = (sender, url, key, payload, timeout)
    if traffic_spec is not None:
        arguments += (traffic_spec, stop_path)
    process = context.Process(target=_send_worker, args=arguments, daemon=True)
    started = time.monotonic()
    reader = None
    events = queue.Queue(maxsize=4)
    head = ResponseHead(0)
    sent = traffic_spec is None
    send_started_at, cooldown_notified = None, False

    def stopped():
        return ((stop_path is not None and Path(stop_path).exists())
                or traffic_spec is not None and (Path(traffic_spec["root"]) / "STOP").exists())

    def notify_traffic(method, *args):
        from .pilot_traffic import SharedTraffic
        try:
            traffic = SharedTraffic(**traffic_spec, operation_deadline=started + timeout, stopped=stopped)
            getattr(traffic, method)(*args)
            return True
        except GuardError as error:
            if error.code in {"TRAFFIC_LOCK_TIMEOUT", "QUEUE_DEADLINE", "CONTROLLER_STOPPED"}:
                _write(Path(traffic_spec["root"]) / "HEALTH_ALERT.json",
                    dict(action="transport_accounting_deferred", phase=method,
                         code=error.code, time=time.time()))
            else:
                SharedTraffic.stop(SimpleNamespace(root=Path(traffic_spec["root"])),
                    "traffic_transport_state_error", phase=method, code=error.code)
            return False
        except Exception as error:
            SharedTraffic.stop(SimpleNamespace(root=Path(traffic_spec["root"])),
                "traffic_transport_state_error", phase=method, error_type=type(error).__name__)
            return False

    def read_events():
        # A pipe can be readable while a large pickled final is still partial.
        # Only fully received events enter this queue, so the deadline handler
        # never blocks in recv() waiting for body bytes.
        try:
            for _ in range(4):
                event = receiver.recv()
                events.put_nowait(event)
                if isinstance(event, TransportResult):
                    return
        except Exception:
            events.put_nowait(None)

    def consume(event):
        nonlocal head, sent, send_started_at, cooldown_notified
        if isinstance(event, RequestStarted):
            sent = True
            send_started_at = event.time
            return None
        if isinstance(event, RequestWritten):
            if traffic_spec is not None and admission_token is not None:
                notify_traffic("started", admission_token)
            return None
        if isinstance(event, ResponseHead):
            head = event
            if event.status == 429 and traffic_spec is not None:
                cooldown_notified = notify_traffic("observe", event)
            return None
        if isinstance(event, TransportResult):
            event = event._replace(send_started_at=send_started_at, cooldown_notified=cooldown_notified)
            if head.status and not event.status:
                return event._replace(status=head.status, retry_after_seconds=head.retry_after_seconds,
                                      retry_after_error=head.retry_after_error)
            if event.status:
                head = ResponseHead(event.status, event.retry_after_seconds, event.retry_after_error)
            return event
        return failure("worker_error")

    def failure(code):
        return TransportResult(head.status, b"", _diagnostic("transport", code),
                               head.retry_after_seconds, head.retry_after_error, True if sent else None,
                               send_started_at, cooldown_notified)

    def finish(code):
        # Inspect completed, already queued events
        # only; never extend the deadline to wait for a body or a worker exit.
        final = None
        for _ in range(4):
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            result = consume(event)
            if isinstance(event, TransportResult):
                final = result
        if final is not None and code != "controller_stopped":
            return final
        return failure(code)

    try:
        process.start()
        sender.close()
        reader = threading.Thread(target=read_events, daemon=True)
        reader.start()
        while time.monotonic() - started < timeout:
            if stopped():
                return finish("controller_stopped")
            remaining = max(0, timeout - (time.monotonic() - started))
            try:
                event = events.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            result = consume(event)
            if result is not None:
                if stopped():
                    return failure("controller_stopped")
                return result
        return finish("timeout")
    except Exception:
        code = "timeout" if time.monotonic() - started >= timeout else "worker_error"
        return finish(code)
    finally:
        sender.close()
        if process.pid is not None:
            if process.is_alive():
                process.terminate()
            process.join(timeout=1)
            if process.is_alive():
                process.kill()
                process.join(timeout=1)
        receiver.close()
        if reader is not None:
            reader.join(timeout=0.1)


def _usage(response):
    """Validate core counts and return a fresh allowlisted accounting object.

    Unknown extensions are ignored, not copied into the ledger. Optional nulls
    remain unknown; details are subsets, never additional billed tokens.
    """
    required = ("prompt_tokens", "completion_tokens", "total_tokens")
    usage = response.get("usage") if isinstance(response, dict) else None

    def fail(code, **fields):
        reported = {key: usage[key] for key in required if isinstance(usage, dict) and key in usage
                    and _integer(usage[key]) and usage[key] <= MAX_DIAGNOSTIC_INTEGER}
        raise DiagnosticError(_diagnostic("usage", code, reported_unvalidated=reported, **fields))

    if not isinstance(response, dict):
        fail("response_type", field="response", value_type=_type_label(response))
    if "usage" not in response:
        fail("usage_missing", field="usage")
    if usage is None:
        fail("usage_null", field="usage", value_type="null")
    if not isinstance(usage, dict):
        fail("usage_type", field="usage", value_type=_type_label(usage))
    for field in required:
        if field not in usage:
            fail("missing_required", field=field)
    for field in required:
        if not _integer(usage[field]):
            fail("invalid_counts", field=field, value_type=_type_label(usage[field]))
    if usage["prompt_tokens"] + usage["completion_tokens"] != usage["total_tokens"]:
        fail("total_mismatch", field="total_tokens")
    normalized = {field: usage[field] for field in required}
    detail_keys = {"prompt_tokens_details": {"cached_tokens", "audio_tokens", "cache_write_tokens",
                                            "image_tokens", "text_tokens"},
                   "completion_tokens_details": {"reasoning_tokens", "audio_tokens",
                                                 "accepted_prediction_tokens", "rejected_prediction_tokens", "text_tokens"}}
    for field, parent in [("prompt_tokens_details", "prompt_tokens"),
                          ("completion_tokens_details", "completion_tokens")]:
        if field not in usage:
            continue
        details = usage[field]
        if details is None:
            normalized[field] = None
            continue
        if not isinstance(details, dict):
            fail("details_type", field=field, value_type=_type_label(details))
        normalized[field] = {}
        for name in sorted(detail_keys[field]):
            if name not in details:
                continue
            if details[name] is None:
                normalized[field][name] = None
                continue
            if not _integer(details[name]):
                fail("details_invalid_counts", field=field, detail_field=name, value_type=_type_label(details[name]))
            if details[name] > usage[parent]:
                fail("details_exceeds_parent", field=field, detail_field=name)
            normalized[field][name] = details[name]
    return normalized


class BudgetProxy:
    """Serialized global budget; estimates are conservative, not a billing cap.

    Admission uses max(cumulative estimates, known actual input) plus this full
    request's estimate. Output reserves the maximum, then settles actual usage.
    Unknown usage is never represented as zero: its record has usage=None and
    the ledger remains locked. Limits may be reduced but never exceed Stage D.
    Any budget admission refusal ends this smoke budget, even if a smaller
    request might fit. Exact exhaustion also locks after returning the last
    admitted response, so the runner can stop before starting another job.
    """
    _request_output_limit = 4096

    def __init__(self, state_dir, *, model, upstream_url, api_key, timeout=120,
                 max_attempts=32, max_input_tokens=300000, max_output_tokens=65536,
                 testing_only=False):
        try:
            if type(testing_only) is not bool or not isinstance(upstream_url, str):
                raise ValueError("target")
            if testing_only:
                parts = urllib.parse.urlsplit(upstream_url)
                if (parts.scheme != "http" or not ipaddress.ip_address(parts.hostname).is_loopback
                        or parts.username or parts.password or parts.query or parts.fragment
                        or parts.path != "/v1/chat/completions" or upstream_url.strip() != upstream_url
                        or parts.port is None or not 0 < parts.port < 65536):
                    raise ValueError("loopback")
            elif upstream_url != PRODUCTION_URL:
                raise ValueError("production")
        except Exception:
            raise GuardError("INVALID_UPSTREAM") from None
        if not isinstance(model, str) or not model or not isinstance(api_key, str) or not api_key:
            raise GuardError("INVALID_CONFIGURATION")
        if any(c in api_key for c in "\r\n"):
            raise GuardError("INVALID_CONFIGURATION")
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 < timeout <= 600:
            raise GuardError("INVALID_TIMEOUT")
        limits = {"attempts": max_attempts, "input": max_input_tokens, "output": max_output_tokens,
                  "per_output": 4096}
        if any(not _integer(v) or not 0 < v <= LIMITS[k] for k, v in limits.items()):
            raise GuardError("INVALID_LIMITS")
        self.model, self.upstream_url, self._key, self.timeout = model, upstream_url, api_key, timeout
        self.policy = {"model": model, "upstream_url": upstream_url, "limits": limits,
                       "encoding": "o200k_base", "overhead": "256+16_per_message",
                       "testing_only": testing_only}
        self.encoding = tiktoken.get_encoding("o200k_base")
        self.directory = Path(state_dir)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "state.json"
        with self._lock():
            if not self.path.exists():
                _write(self.path, {"version": 1, "policy": self.policy, "attempts": 0,
                                  "local_rejections": 0, "estimated_input_tokens": 0,
                                  "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
                                  "pending": None, "locked": None, "records": []})
            self._load()

    @contextmanager
    def _lock(self):
        fd = os.open(self.directory / "state.lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _load(self):
        try:
            state = _decode(self.path.read_bytes())
            counters = ["attempts", "local_rejections", "estimated_input_tokens",
                        "prompt_tokens", "completion_tokens", "total_tokens"]
            if (type(state["version"]) is not int or state["version"] != 1 or state["policy"] != self.policy
                    or any(not _integer(state[k]) for k in counters)
                    or state["attempts"] > self.policy["limits"]["attempts"]
                    or not isinstance(state["records"], list)
                    or state["locked"] not in {None, "usage_unknown", "budget_exceeded", "budget_exhausted",
                                               "controller_stopped"}
                    or state["prompt_tokens"] + state["completion_tokens"] != state["total_tokens"]):
                raise ValueError("state")
            records = state["records"]
            pending = state["pending"]
            if len(records) + (pending is not None) != state["attempts"]:
                raise ValueError("attempts")
            totals = {k: 0 for k in ["prompt_tokens", "completion_tokens", "total_tokens"]}
            estimate = 0
            for index, record in enumerate(records, 1):
                if (not _integer(record["attempt"]) or record["attempt"] != index
                        or not _integer(record["estimated_input_tokens"])
                        or not _integer(record["max_output_tokens"])
                        or not 0 < record["max_output_tokens"] <= 4096
                        or not _integer(record["status"]) or record["status"] > 599
                        or record["outcome"] not in {"settled", "usage_unknown", "budget_exceeded"}):
                    raise ValueError("record")
                estimate += record["estimated_input_tokens"]
                if record["usage"] is not None:
                    _usage({"usage": record["usage"]})
                    for key in totals:
                        totals[key] += record["usage"][key]
                elif state["locked"] != "usage_unknown":
                    raise ValueError("unknown")
            if pending is not None:
                if (not _integer(pending["attempt"]) or pending["attempt"] != state["attempts"]
                        or not _integer(pending["estimated_input_tokens"])
                        or not _integer(pending["max_output_tokens"])
                        or not 0 < pending["max_output_tokens"] <= 4096):
                    raise ValueError("pending")
                estimate += pending["estimated_input_tokens"]
            if estimate != state["estimated_input_tokens"] or any(state[k] != v for k, v in totals.items()):
                raise ValueError("totals")
        except Exception:
            raise GuardError("STATE_INVALID", 503) from None
        # The lock is held across send/settlement: an observed pending entry is
        # an interrupted previous owner, never an active cooperating request.
        if state["pending"] is not None and state["locked"] != "usage_unknown":
            state["locked"] = "usage_unknown"
            _write(self.path, state)
        elif state["locked"] is None and self._exhausted(state):
            state["locked"] = "budget_exhausted"
            _write(self.path, state)
        elif state["locked"] is None and self._stop_requested():
            state["locked"] = "controller_stopped"
            _write(self.path, state)
        return state

    def _stop_requested(self):
        return (self.directory / "STOP").exists()

    def stop_new_requests(self, reason="controller_stopped"):
        """Durably stop admission without waiting for the network/state flock.

        Safe in runner finally blocks and idempotent across processes. An
        already admitted request may settle; a crashed pending request remains
        usage_unknown. snapshot() still waits for active settlement, whereas
        this method only writes the independent, credential-free stop marker.
        """
        if reason != "controller_stopped":
            raise GuardError("INVALID_STOP_REASON")
        _write(self.directory / "STOP", {"reason": "controller_stopped"})

    def _exhausted(self, state):
        limits = self.policy["limits"]
        return (state["attempts"] >= limits["attempts"]
                or max(state["estimated_input_tokens"], state["prompt_tokens"]) >= limits["input"]
                or state["completion_tokens"] >= limits["output"])

    def snapshot(self):
        with self._lock():
            return self._load()

    def reject(self, code="INVALID_REQUEST", status=400):
        with self._lock():
            state = self._load()
            state["local_rejections"] += 1
            _write(self.path, state)
        raise GuardError(code, status)

    def _payload(self, value):
        if not isinstance(value, dict):
            raise ValueError("parameters")
        if "session_id" in value:
            session_id = value["session_id"]
            if not isinstance(session_id, str) or re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id) is None:
                raise ValueError("session_id")
            # Controller metadata only: never route, persist, or bill this field.
            value = {key: item for key, item in value.items() if key != "session_id"}
        allowed = {"messages", "model", "reasoning_effort", "max_completion_tokens", "max_tokens",
                   "response_format", "stream", "n"}
        if value.keys() - allowed or value.get("model") != self.model:
            raise ValueError("parameters")
        if value.get("reasoning_effort") != "medium" or value.get("stream", False) is not False:
            raise ValueError("configuration")
        if type(value.get("n", 1)) is not int or value.get("n", 1) != 1:
            raise ValueError("n")
        keys = [k for k in ("max_tokens", "max_completion_tokens") if k in value]
        if len(keys) != 1 or not _integer(value[keys[0]]) or not 0 < value[keys[0]] <= self._request_output_limit:
            raise ValueError("output")
        messages = value.get("messages")
        if not isinstance(messages, list) or not messages:
            raise ValueError("messages")
        for message in messages:
            if (not isinstance(message, dict) or message.keys() - {"role", "content", "name"}
                    or message.get("role") not in {"system", "developer", "user", "assistant"}
                    or ("name" in message and not isinstance(message["name"], str))):
                raise ValueError("message")
            content = message.get("content")
            if not isinstance(content, str):
                if not isinstance(content, list) or not content or any(
                        not isinstance(p, dict) or set(p) != {"type", "text"}
                        or p["type"] != "text" or not isinstance(p["text"], str) for p in content):
                    raise ValueError("content")
        if "response_format" in value:
            fmt = value["response_format"]
            if not isinstance(fmt, dict) or fmt.get("type") not in {"text", "json_object", "json_schema"}:
                raise ValueError("format")
            if fmt["type"] == "json_schema":
                schema = fmt.get("json_schema")
                if (set(fmt) != {"type", "json_schema"} or not isinstance(schema, dict)
                        or schema.keys() - {"name", "description", "schema", "strict"}
                        or not isinstance(schema.get("name"), str)
                        or not isinstance(schema.get("schema"), dict)
                        or ("strict" in schema and type(schema["strict"]) is not bool)
                        or ("description" in schema and not isinstance(schema["description"], str))):
                    raise ValueError("schema")
            elif set(fmt) != {"type"}:
                raise ValueError("format")
        payload = dict(value, n=1, stream=False)
        raw = _encode(payload)
        if len(raw) > MAX_BODY:
            raise ValueError("size")
        estimate = len(self.encoding.encode(raw.decode(), disallowed_special=())) + 256 + 16 * len(messages)
        return raw, estimate, value[keys[0]]

    def forward(self, value):
        """Return original JSON bytes after usage settlement, without protocol repair."""
        try:
            payload, estimate, maximum = self._payload(value)
        except Exception:
            self.reject()
        with self._lock():
            state = self._load()
            limits = self.policy["limits"]
            code = None
            if state["locked"]:
                code = {"usage_unknown": "USAGE_UNKNOWN", "controller_stopped": "CONTROLLER_STOPPED"}.get(
                    state["locked"], "BUDGET_EXCEEDED")
            elif (state["attempts"] >= limits["attempts"]
                  or max(state["estimated_input_tokens"], state["prompt_tokens"]) + estimate > limits["input"]
                  or state["completion_tokens"] + maximum > limits["output"]):
                code = "BUDGET_EXCEEDED"
                state["locked"] = "budget_exhausted"
            if code:
                state["local_rejections"] += 1
                _write(self.path, state)
                raise GuardError(code, 503)
            state["attempts"] += 1
            state["estimated_input_tokens"] += estimate
            pending = {"attempt": state["attempts"], "estimated_input_tokens": estimate,
                       "max_output_tokens": maximum}
            state["pending"] = pending
            _write(self.path, state)
            result = _send(self.upstream_url, self._key, payload, self.timeout)
            # Existing zero-cost mocks return (status, body); the real transport
            # additionally carries fixed, content-free diagnostic information.
            if not isinstance(result, TransportResult):
                result = TransportResult(*result)
            status, raw, diagnostic = result.status, result.body, result.diagnostic
            usage = None
            if diagnostic is None and status != 200:
                diagnostic = _diagnostic("transport", "non_200_status" if status else "transport_unknown")
            if diagnostic is None:
                try:
                    usage = _usage(_decode_response(raw))
                except DiagnosticError as error:
                    diagnostic = error.diagnostic
                except Exception:
                    diagnostic = _diagnostic("decode", "validation_error")
            if diagnostic is not None:
                state["locked"] = "usage_unknown"
            if usage is not None:
                for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                    state[key] += usage[key]
                if (state["prompt_tokens"] > limits["input"] or state["completion_tokens"] > limits["output"]
                        or usage["completion_tokens"] > maximum):
                    state["locked"] = "budget_exceeded"
            state["records"].append(dict(pending, status=status, usage=usage,
                                          outcome=state["locked"] or "settled", diagnostic=diagnostic))
            state["pending"] = None
            if state["locked"] is None and self._exhausted(state):
                state["locked"] = "budget_exhausted"
            elif state["locked"] is None and self._stop_requested():
                state["locked"] = "controller_stopped"
            _write(self.path, state)
            if state["locked"] in {"usage_unknown", "budget_exceeded"}:
                raise GuardError("UPSTREAM_USAGE_UNKNOWN" if usage is None else "UPSTREAM_BUDGET_EXCEEDED", 502)
            return raw


def make_server(proxy, port=0):
    """Bind loopback only; caller owns serve_forever/shutdown/server_close."""
    if getattr(proxy, 'queue_wait_mode', None) is not None:
        raise GuardError('QUEUE_MODE_REQUIRES_SUPERVISED_DIRECT_CALL', 503)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(10)

        def do_POST(self):
            try:
                if self.path != "/v1/chat/completions":
                    proxy.reject("UNKNOWN_PATH", 404)
                lengths = self.headers.get_all("Content-Length", [])
                if (len(lengths) != 1 or not lengths[0].isdigit()
                        or not 0 < int(lengths[0]) <= MAX_BODY or self.headers.get("Transfer-Encoding")):
                    proxy.reject()
                length = int(lengths[0])
                try:
                    body = self.rfile.read(length)
                    if len(body) != length:
                        raise ValueError("body")
                    value = _decode(body)
                except Exception:
                    proxy.reject()
                if getattr(proxy, "requires_request_id", False):
                    ids = self.headers.get_all("X-SkillHorizon-Request-ID", [])
                    if len(ids) != 1:
                        proxy.reject("INVALID_REQUEST_ID")
                    raw = proxy.forward(value, request_id=ids[0])
                else:
                    raw = proxy.forward(value)
                status = 200
            except GuardError as error:
                raw, status = _encode({"error": {"message": error.code, "type": "provider_guard"}}), error.status
            except Exception:
                raw, status = b'{"error":{"message":"GUARD_FAILURE"}}', 503
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError, TimeoutError):
                pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("state_dir", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--upstream-url", default=PRODUCTION_URL, choices=[PRODUCTION_URL])
    parser.add_argument("--key-env", default="OPENLUX_API_KEY")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--timeout", type=float, default=120)
    args = parser.parse_args()
    try:
        key = os.environ.pop(args.key_env, "")
        proxy = BudgetProxy(args.state_dir, model=args.model, upstream_url=args.upstream_url,
                            api_key=key, timeout=args.timeout)
        server = make_server(proxy, args.port)
        print(json.dumps({"api_base": f"http://127.0.0.1:{server.server_port}/v1"}), flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    except KeyboardInterrupt:
        pass
    except Exception:
        raise SystemExit("Provider guard stopped; configuration or ledger unavailable") from None


if __name__ == "__main__":
    main()
