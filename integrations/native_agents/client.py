"""Public standard-library client. A failed mutation is never retried."""
import argparse
import json
import os
from pathlib import Path
import sys
from urllib import error, parse, request


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, url=None):
        self.url = (url or os.environ.get("SKILLHORIZON_URL", "http://business:8765")).rstrip("/")
        self.opener = request.build_opener(request.ProxyHandler({}), NoRedirect())

    def request(self, path, body=None):
        payload = json.dumps(body, allow_nan=False).encode() if body is not None else None
        req = request.Request(self.url + path, data=payload, headers={"Content-Type": "application/json"})
        try:
            with self.opener.open(req, timeout=180) as response:
                return json.load(response)
        except error.HTTPError as exc:
            return json.loads(exc.read())
        except (OSError, ValueError) as exc:
            raise RuntimeError("Delivery uncertain; do not retry this call automatically") from exc

    def tools(self):
        return self.request("/tools")

    def schema(self, name):
        return self.request("/schema/" + parse.quote(name, safe=""))

    def call(self, name, arguments, *, skill_run_id=None):
        body = {"name": name, "arguments": arguments}
        if skill_run_id is not None:
            body["skill_run_id"] = skill_run_id
        return self.request("/call", body)

    def call_raw(self, name, arguments_json, *, skill_run_id=None):
        body = {"name": name, "arguments_json": arguments_json}
        if skill_run_id is not None:
            body["skill_run_id"] = skill_run_id
        return self.request("/call", body)


def main(argv=None):
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list")
    schema = sub.add_parser("schema")
    schema.add_argument("name")
    call = sub.add_parser("call")
    call.add_argument("name")
    call.add_argument("arguments", help="JSON, @FILE, or - for stdin")
    call.add_argument("--skill-run-id")
    args = parser.parse_args(argv)
    client = Client()
    if args.command == "list":
        result = client.tools()
    elif args.command == "schema":
        result = client.schema(args.name)
    else:
        raw = sys.stdin.read() if args.arguments == "-" else (
            Path(args.arguments[1:]).read_text() if args.arguments.startswith("@") else args.arguments)
        result = client.call_raw(args.name, raw, skill_run_id=args.skill_run_id)
    print(json.dumps(result, ensure_ascii=False))
    value = result.get("result", result)
    failed = "error" in result or value.get("ok") is False or value.get("status") == "error"
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
