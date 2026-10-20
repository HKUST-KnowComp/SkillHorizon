"""Standalone public business client; standard library only, with no retries."""

import argparse
import http.client
import json
import math
import os
import sys
from urllib import error, parse, request


MISSING = object()
DECLARATION_PROTOCOL = "skill_declaration_v1"


class ClientError(Exception):
    """Transport, protocol or local input failure, not a business result."""


class ExecutionUncertain(ClientError):
    """A call may have committed; it must not be automatically resubmitted."""


class DeclarationUncertain(ClientError):
    """A declaration receipt is unknown; no business execution is implied."""

    declaration_uncertain = True
    execution_uncertain = False


class HTTPError(ClientError):
    def __init__(self, status, body, *, execution_uncertain=False, declaration_uncertain=False, new_protocol=False):
        if execution_uncertain or declaration_uncertain:
            try:
                # Match the exact gateway envelope, including no duplicate keys.
                fields = json.loads(body, object_pairs_hook=list, parse_constant=_reject_constant)
            except ValueError:
                pass
            else:
                known = ["PUBLIC_SCHEMA", "SEALED", "BUSINESS_CALL_BUDGET"]
                if new_protocol:
                    known.extend(["INVALID_REQUEST", "DECLARATION_EVENT_TOO_LARGE"])
                if any(fields == [("error", code)] for code in known):
                    execution_uncertain = False
                    declaration_uncertain = False
                if new_protocol and fields == [("error", "DECLARATION_RECORDING_INVALID")]:
                    execution_uncertain = False
        self.status = status
        self.body = body
        self.execution_uncertain = execution_uncertain
        self.declaration_uncertain = declaration_uncertain
        message = "HTTP {}: {}".format(status, body)
        if execution_uncertain:
            message += "; execution uncertain: the call may have committed; do not retry automatically"
        if declaration_uncertain:
            message += "; declaration receipt uncertain; no business execution implied; do not retry automatically"
        super().__init__(message)


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _reject_constant(value):
    raise ValueError("Non-finite JSON number: " + value)


def _decode(value):
    def pairs(items):
        result = {}
        for key, item in items:
            if key in result:
                raise ValueError("Duplicate JSON key")
            result[key] = item
        return result
    return json.loads(value, parse_constant=_reject_constant, object_pairs_hook=pairs)


class Client:
    def __init__(self, base_url=None, timeout=120, *, skill_protocol=None):
        if skill_protocol is None:
            skill_protocol = os.environ.get("SKILLHORIZON_PROTOCOL", "")
        self.skill_protocol = skill_protocol
        if base_url is None:
            base_url = os.environ.get("SKILLHORIZON_URL", "http://business:8765")
        if not isinstance(base_url, str) or any(c.isspace() for c in base_url):
            raise ValueError("base_url must be an HTTP(S) URL without whitespace")
        parts = parse.urlsplit(base_url)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or parts.username is not None or parts.password is not None
                or parts.query or parts.fragment):
            raise ValueError("base_url must be HTTP(S), without credentials, query or fragment")
        parts.port  # Validate malformed port values before any request.
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("timeout must be a finite positive number")
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("timeout must be a finite positive number")
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Internal business traffic must not inherit host proxy credentials/routing.
        self._opener = request.build_opener(request.ProxyHandler({}), _NoRedirect())

    def _failure(self, message, mutation, declaration=False):
        if declaration:
            return DeclarationUncertain(message + "; declaration receipt uncertain; no business execution implied; do not retry automatically")
        if mutation:
            return ExecutionUncertain(
                message + "; execution uncertain: the call may have committed; do not retry automatically"
            )
        return ClientError(message)

    def _request(self, path, payload=None, *, declaration=False):
        mutation = payload is not None
        data = None
        if mutation:
            data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        req = request.Request(self.base_url + path, data=data,
                              headers={"Accept": "application/json", "Content-Type": "application/json"},
                              method="POST" if mutation else "GET")
        try:
            with self._opener.open(req, timeout=self.timeout) as response:
                status = response.status
                raw = response.read()
        except error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except (OSError, http.client.HTTPException) as read_error:
                body = "response body unavailable ({})".format(type(read_error).__name__)
            finally:
                exc.close()
            raise HTTPError(exc.code, body, execution_uncertain=mutation and not declaration,
                            declaration_uncertain=declaration, new_protocol=self.skill_protocol == DECLARATION_PROTOCOL) from None
        except (error.URLError, OSError, http.client.HTTPException) as exc:
            raise self._failure("Transport failure: {}: {}".format(type(exc).__name__, exc), mutation, declaration) from None
        if status != 200:
            raise HTTPError(status, raw.decode("utf-8", errors="replace"), execution_uncertain=mutation and not declaration,
                            declaration_uncertain=declaration, new_protocol=self.skill_protocol == DECLARATION_PROTOCOL)
        try:
            return _decode(raw.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError) as exc:
            raise self._failure("Invalid JSON response: " + str(exc), mutation, declaration) from None

    def tools(self):
        value = self._request("/tools")
        if (not isinstance(value, dict) or not isinstance(value.get("tools"), list)
                or any(not isinstance(tool, dict) or not isinstance(tool.get("name"), str)
                       for tool in value["tools"])):
            raise ClientError("Invalid /tools response: expected {tools: [native tool definitions]}")
        return value

    def schema(self, name):
        matches = [tool for tool in self.tools()["tools"] if tool["name"] == name]
        if len(matches) != 1:
            raise ClientError("Unknown or ambiguous tool name: " + str(name))
        return matches[0]

    def call(self, name, args, *, skill_run_id=MISSING):
        if not isinstance(name, str) or not name:
            raise ValueError("Tool name must be a nonempty string")
        if not isinstance(args, dict):
            raise ValueError("Tool arguments must be a JSON object")
        payload = {"name": name, "arguments": args}
        if skill_run_id is not MISSING:
            payload["skill_run_id"] = skill_run_id
        declaration = self.skill_protocol == DECLARATION_PROTOCOL and name in {"begin_skill", "finish_skill"}
        value = self._request("/call", payload, declaration=declaration)
        if (not isinstance(value, dict) or "result" not in value
                or type(value.get("sequence")) is not int):
            raise self._failure("Invalid /call response: expected result and integer sequence", True, declaration)
        return value["result"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default=None)
    parser.add_argument("--timeout", type=float, default=120)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List all native tool definitions")
    schema_parser = commands.add_parser("schema")
    schema_parser.add_argument("name")
    call_parser = commands.add_parser("call")
    call_parser.add_argument("name")
    call_parser.add_argument("arguments", help="JSON object, @FILE or - for stdin")
    call_parser.add_argument("--skill-run-id", default=MISSING)
    options = parser.parse_args(argv)
    try:
        client = Client(options.url, options.timeout)
        if options.command == "list":
            value = client.tools()
        elif options.command == "schema":
            value = client.schema(options.name)
        else:
            text = options.arguments
            if text == "-":
                text = sys.stdin.read()
            elif text.startswith("@"):
                with open(text[1:], encoding="utf-8") as stream:
                    text = stream.read()
            value = client.call(options.name, _decode(text), skill_run_id=options.skill_run_id)
        print(json.dumps(value, ensure_ascii=False, allow_nan=False))
        return 0
    except (ClientError, ValueError, OSError) as exc:
        print(json.dumps({"error": type(exc).__name__, "message": str(exc)},
                         ensure_ascii=False, allow_nan=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
