"""Two-stage loopback actor fixture derived only from installed public surfaces."""

from http.server import BaseHTTPRequestHandler
import json
from pathlib import Path
import re
import threading

from jsonschema import Draft202012Validator


CONFIRMATION = ('Are you sure you want to mark the task as complete? '
    "This will trigger your solution to be graded and you won't be able to "
    'make any further corrections. If so, include "task_complete": true in your JSON response again.')


def fixture(domain, public_root, tools):
    """tools must be the public /tools response, never a reference trace."""
    public_root = Path(public_root)
    skill_paths = sorted((public_root / "skills").glob("*/SKILL.md"))
    if not skill_paths or domain not in {"C25", "C06", "C09", "C11", "C50", "DataOps", "FieldOps"}:
        raise ValueError("Missing public skill or unsupported probe domain")
    skill = skill_paths[0].parent.name
    if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", skill):
        raise ValueError("Unsafe public skill name")
    action = "read_project" if domain in {"DataOps", "FieldOps"} else "read_task"
    matches = [entry for entry in tools["tools"] if entry.get("name") == action]
    if len(matches) != 1:
        raise ValueError("Public read operation absent or ambiguous")
    schema = matches[0].get("parameters", matches[0].get("inputSchema"))
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({})
    run_id = "native-interface-" + domain
    marker = "SH_PUBLIC_INTERFACE_OK_" + domain
    begin = {"run_id": run_id, "skill_id": skill}
    finish = {"run_id": run_id, "status": "completed"}
    code = "\n".join([
        "from pathlib import Path", "from client import Client",
        f"assert Path('/skills/{skill}/SKILL.md').read_text().strip()",
        "c = Client(timeout=45)",
        f"assert c.call('begin_skill', {begin!r}) == {{'run_id': {run_id!r}, 'status': 'open'}}",
        f"result = c.call({action!r}, {{}}, skill_run_id={run_id!r})",
        "assert isinstance(result, dict) and not result.get('error') and not result.get('isError')",
        f"assert c.call('finish_skill', {finish!r}) == {{'run_id': {run_id!r}, 'status': 'completed'}}",
        f"print({marker!r}, flush=True)",
    ])
    return {"domain": domain, "skill_id": skill, "skill_path": "/skills/" + skill + "/SKILL.md",
        "run_id": run_id, "action": {"name": action, "arguments": {}},
        "begin": begin, "finish": finish, "marker": marker,
        "command": "python - <<'SH_PUBLIC_PROBE'\n" + code + "\nSH_PUBLIC_PROBE\n",
        "public_sources": {"skill": str(skill_paths[0]), "operation": matches[0],
            "arguments": "Empty object validated against public tool schema; no private fields",
            "run_id": "Controller-chosen public declaration label"}}


class ActorState:
    def __init__(self, definition):
        self.definition = definition
        self.requests = []
        self.failures = []
        self.lock = threading.Lock()

    def respond(self, payload):
        with self.lock:
            ordinal = len(self.requests) + 1
            self.requests.append(payload)
            try:
                if ordinal == 1:
                    commands = [{"keystrokes": self.definition["command"], "duration": 60}]
                elif ordinal == 2:
                    messages = payload["messages"]
                    message = messages[-1]
                    content = message.get("content")
                    if isinstance(content, list):
                        if any(part.get("type") != "text" for part in content):
                            raise ValueError("Unexpected native observation content")
                        content = "".join(part["text"] for part in content)
                    if (message.get("role") != "user" or not isinstance(content, str)
                            or not content.startswith("Current terminal state:\n")
                            or not content.endswith(CONFIRMATION)):
                        raise ValueError("Missing native completion confirmation")
                    observation = content[len("Current terminal state:\n"):-len(CONFIRMATION)]
                    lines = [line.rstrip("\r") for line in observation.splitlines()]
                    if self.definition["marker"] not in lines:
                        raise ValueError("Public operation did not produce its terminal marker")
                    commands = []
                else:
                    raise ValueError("Unexpected extra actor request")
                return {"analysis": "Public interface qualification", "plan": "Fixed read-only check",
                        "commands": commands, "task_complete": True}
            except Exception as error:
                self.failures.append({"ordinal": ordinal, "type": type(error).__name__})
                raise


class ActorHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            if self.path != "/v1/chat/completions":
                raise ValueError("Unexpected fixture endpoint")
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            content = self.server.actor.respond(payload)
            response = {"id": "native-fixture", "object": "chat.completion", "created": 1,
                "model": "gpt-5.6-sol", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": json.dumps(content)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}
            status = 200
        except Exception:
            response, status = {"error": {"message": "Native interface fixture rejected request"}}, 400
        body = json.dumps(response).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
