from copy import deepcopy
import http.client
import json
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .config import RunConfig, validate_generation_settings, validate_url

TRANSIENT_HTTP_STATUSES = {500, 502, 503, 504}
TRANSIENT_HTTP_CLASSIFICATION = "transient_http_v1"


class ProviderFailure(RuntimeError):
    def __init__(self, code, message, details):
        super().__init__(message)
        self.code, self.message, self.details = code, message, details

    def to_dict(self):
        return {"code": self.code, "message": self.message, "details": self.details}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def prompt_rejection_code(details):
    code = details.get("provider_code")
    if code is not None:
        return code
    message = details.get("provider_message", "")
    if isinstance(message, str):
        try:
            embedded = json.loads(message)
            if isinstance(embedded, dict):
                embedded = embedded.get("error", embedded)
                if isinstance(embedded, dict) and embedded.get("code") == "invalid_prompt":
                    return "invalid_prompt"
        except ValueError:
            # Known truncated upstream JSON, direct or wrapped in an error object.
            if re.match(r'^\s*\{\s*(?:"error"\s*:\s*\{\s*)?"code"\s*:\s*"invalid_prompt"\s*,\s*"message"\s*:\s*"Invalid prompt:', message):
                return "invalid_prompt"
    return None


def retryable_prompt_rejection(error):
    if not isinstance(error, dict):
        return False
    details = error.get("details", {})
    if not isinstance(details, dict):
        return False
    return (error.get("code") == "PROVIDER_HTTP_ERROR"
            and details.get("http_status") == 400
            and details.get("provider_type") == "invalid_request_error"
            and prompt_rejection_code(details) == "invalid_prompt")


def retryable_transport_failure(error):
    """Transient provider failures, never an uncertain local tool invocation."""
    if not isinstance(error, dict):
        return False
    details = error.get("details")
    if (not isinstance(details, dict) or type(details.get("transport_attempt_count")) is not int
            or details["transport_attempt_count"] != 1):
        return False
    if error.get("code") == "PROVIDER_HTTP_ERROR":
        # Version new observations, so historical terminal HTTP errors still replay unchanged.
        return (details.get("classification") == TRANSIENT_HTTP_CLASSIFICATION
                and type(details.get("http_status")) is int
                and details["http_status"] in TRANSIENT_HTTP_STATUSES)
    if error.get("code") != "PROVIDER_TRANSPORT_ERROR":
        return False
    transient = {"RemoteDisconnected", "IncompleteRead", "TimeoutError",
                 "ConnectionResetError", "ConnectionAbortedError", "BrokenPipeError",
                 "ConnectionRefusedError", "SSLEOFError", "SSLZeroReturnError"}
    kind = details.get("exception_type")
    return kind in transient or (kind == "URLError" and details.get("reason_type") in transient)


class FixtureTransport:
    """A local response sequence; never represents an external model."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = iter(deepcopy(responses))

    @property
    def description(self) -> dict[str, Any]:
        return {"kind": "fixture"}

    def send(self, payload: dict) -> tuple[dict, dict]:
        try:
            response = next(self.responses)
        except StopIteration as exc:
            raise ProviderFailure(
                "FIXTURE_EXHAUSTED", "No fixture response remains", {}
            ) from exc
        return response, {"kind": "fixture", "attempts": 0}


class DelegatingTransport:
    """A runner-owned wrapper; provenance comes from its delegate, never response metadata."""

    @property
    def replaying(self):
        return getattr(self.live, "replaying", False)


def is_live_http(transport):
    if getattr(transport, "replaying", False):
        return False
    if isinstance(transport, DelegatingTransport):
        return is_live_http(transport.live)
    return isinstance(transport, HttpTransport)


class HttpTransport:
    """One Responses request per turn. No hashes, resume protocol or hidden retries."""

    def __init__(self, base_url: str, api_key: str, config: RunConfig) -> None:
        validate_generation_settings(config.model, config.reasoning_effort)
        self._base_url = validate_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip():
            raise ValueError("A provider credential is required")
        self._api_key = api_key
        self._timeout_seconds = config.timeout_seconds
        self._opener = urllib.request.build_opener(_NoRedirect)

    @property
    def description(self) -> dict[str, Any]:
        return {
            "kind": "external",
            "base_url": self._base_url,
            "retries": 0,
            "timeout_seconds": self._timeout_seconds,
        }

    def _perform_request(self, payload):
        request = urllib.request.Request(self._base_url + "/responses",
            data=json.dumps(payload, ensure_ascii=True, allow_nan=False).encode("utf-8"),
            headers={"Authorization": "Bearer " + self._api_key, "Content-Type": "application/json"}, method="POST")
        started = time.monotonic()
        phase, status, headers = "open_or_wait_for_headers", None, {}
        headers_received = None
        try:
            with self._opener.open(request, timeout=self._timeout_seconds) as response:
                headers_received = time.monotonic()
                status, headers = int(response.status), self._trace_headers(response.headers)
                phase = "read_response_body"
                body = response.read()
        except urllib.error.HTTPError as exc:
            details = {"http_status": exc.code, "transport_attempt_count": 1,
                       "latency_seconds": time.monotonic() - started,
                       "response_headers": self._trace_headers(exc.headers)}
            if exc.code in TRANSIENT_HTTP_STATUSES:
                details["classification"] = TRANSIENT_HTTP_CLASSIFICATION
            try:
                error = json.loads(exc.read()).get("error", {})
                for key in ("type", "code", "message", "param"):
                    if isinstance(error.get(key), str):
                        details["provider_" + key] = self._redact(error[key])[:1500]
                code = prompt_rejection_code(details)
                if code is not None and "provider_code" not in details:
                    details["provider_code"] = code
                    details["provider_code_source"] = "embedded_message_prefix"
                message = details.get("provider_message", "")
                match = re.search(r"\(request id:\s*([A-Za-z0-9_-]{1,128})\)", message)
                if match:
                    details["request_id"] = match.group(1)
            except (ValueError, AttributeError, TypeError):
                pass
            except (http.client.HTTPException, OSError) as body_error:
                # The received HTTP status still governs retries even if its body breaks.
                details["error_body_read_error"] = type(body_error).__name__
                details["failure_phase"] = "read_http_error_body"
            finally:
                details["latency_seconds"] = time.monotonic() - started
                exc.close()
            raise ProviderFailure("PROVIDER_HTTP_ERROR", f"Provider HTTP error {exc.code}", details) from exc
        except (urllib.error.URLError, http.client.HTTPException, OSError) as exc:
            # urllib does not expose DNS/TCP/TLS/send separately before headers arrive.
            details = {"exception_type": type(exc).__name__, "transport_attempt_count": 1,
                       "failure_phase": phase, "latency_seconds": time.monotonic() - started}
            if status is not None:
                details.update(http_status=status, response_headers=headers,
                               time_to_headers_seconds=headers_received - started)
            if isinstance(exc, urllib.error.URLError):
                details["reason_type"] = type(exc.reason).__name__
            raise ProviderFailure("PROVIDER_TRANSPORT_ERROR", "Provider transport failed; remote consumption is unknown",
                                  details) from exc
        metadata = {"http_status": status, "transport_attempt_count": 1, "latency_seconds": time.monotonic() - started,
                    "response_headers": self._trace_headers(response.headers)}
        if status != 200:
            raise ProviderFailure("PROVIDER_HTTP_ERROR", f"Provider HTTP status {status} is not a completed synchronous response", metadata)
        try:
            raw = json.loads(body)
        except (ValueError, UnicodeError) as exc:
            raise ProviderFailure("ACCEPTED_RESPONSE_PARSE_ERROR", "Provider returned an invalid JSON body", metadata) from exc
        if not isinstance(raw, dict):
            raise ProviderFailure("ACCEPTED_RESPONSE_SCHEMA_ERROR", "Provider response must be an object", metadata)
        return raw, metadata

    def _redact(self, value):
        value = value.replace(self._api_key, "[REDACTED]")
        return re.sub(r"(?i)(?:sk-[\w-]+|bearer\s+\S+)", "[REDACTED]", value)

    def _trace_headers(self, headers):
        result = {}
        for name in ("x-request-id", "request-id", "openai-request-id", "retry-after"):
            value = headers.get(name) if headers is not None else None
            if isinstance(value, str):
                result[name] = self._redact(value)[:256]
        return result

    def send(self, payload: dict) -> tuple[dict, dict]:
        # Generation settings live only in the runner's serialized payload.
        validate_generation_settings(payload["model"], payload["reasoning"]["effort"])
        raw, metadata = self._perform_request(payload)
        return raw, {**metadata, **self.description}


def fixture_response(
    model: str,
    *,
    calls: list[dict] | None = None,
    input_tokens: int = 8000,
    output_tokens: int = 64,
    status: str = "completed"
) -> dict:
    return {
        "id": "fixture-response",
        "model": model,
        "status": status,
        "error": None,
        "output": calls or [],
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        },
        "incomplete_details": {"reason": "max_output_tokens"}
        if status == "incomplete"
        else None,
    }
