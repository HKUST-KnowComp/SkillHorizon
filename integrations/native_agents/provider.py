"""Native-protocol credential relay, not a model/controller implementation.

Provider keys remain in the launcher process. Sandboxes receive an expiring
per-run relay token. Requests and SSE bodies pass through without translation.
"""
from contextlib import contextmanager, suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import secrets
import threading
from urllib import error, parse, request

from .common import decode


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def validate_base(base):
    parsed = parse.urlsplit(base)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path.rstrip("/") not in ("", "/v1")):
        raise ValueError("Use an HTTPS native API origin or /v1 base without credentials")
    return base.rstrip("/").removesuffix("/v1")


@contextmanager
def relay(base, key, model, protocol):
    base = validate_base(base)
    token = secrets.token_urlsafe(32)
    allowed = ({"/v1/messages", "/v1/messages/count_tokens",
                "/v1/messages?beta=true", "/v1/messages/count_tokens?beta=true"} if protocol == "anthropic" else
               {"/v1/responses", "/v1/responses/compact", "/v1/chat/completions"})
    opener = request.build_opener(request.ProxyHandler({}), NoRedirect())
    state = {"requests": 0, "transport_errors": 0}
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply_error(self, status):
            data = b'{"error":{"message":"Native relay request rejected"}}'
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            supplied = self.headers.get("x-api-key", "") or self.headers.get("Authorization", "").removeprefix("Bearer ")
            if not hmac.compare_digest(supplied, token) or self.path not in allowed:
                self.reply_error(403)
                return
            try:
                self.connection.settimeout(180)
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32 * 1024 * 1024 or self.headers.get("Transfer-Encoding"):
                    raise ValueError("Invalid framing")
                body = self.rfile.read(size)
                payload = decode(body)
                if not isinstance(payload, dict) or payload.get("model") != model:
                    raise ValueError("Model differs from profile")
            except (ValueError, UnicodeError, OSError):
                self.reply_error(400)
                return
            headers = {"Content-Type": "application/json"}
            for name in ("anthropic-version", "anthropic-beta", "OpenAI-Beta", "Accept"):
                if self.headers.get(name):
                    headers[name] = self.headers[name]
            if protocol == "anthropic":
                headers["x-api-key"] = key
            else:
                headers["Authorization"] = "Bearer " + key
            with lock:
                state["requests"] += 1
            req = request.Request(base + self.path, data=body, headers=headers)
            response = None
            sent_headers = False
            try:
                try:
                    response = opener.open(req, timeout=180)
                except error.HTTPError as exc:
                    response = exc
                self.send_response(response.status)
                self.send_header("Content-Type", response.headers.get("Content-Type", "application/json"))
                for name, value in response.headers.items():
                    lowered = name.lower()
                    if (lowered in {"retry-after", "request-id", "x-request-id"}
                            or lowered.startswith(("x-ratelimit-", "anthropic-ratelimit-"))):
                        self.send_header(name, value)
                self.send_header("Connection", "close")
                self.end_headers()
                sent_headers = True
                self.close_connection = True
                while True:
                    chunk = response.read1(65536)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except (OSError, error.URLError):
                with lock:
                    state["transport_errors"] += 1
                if not sent_headers:
                    with suppress(OSError):
                        self.reply_error(502)
                self.close_connection = True
            finally:
                if response is not None:
                    response.close()

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, token, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
