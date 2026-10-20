"""Observational workflow bookkeeping for domain runtimes, never a business gate."""

from skillhorizon.host_runtime import require_supported_python, check_python_executable

require_supported_python()

from copy import deepcopy
import json
import subprocess
import sys
import tempfile


def skill_receipt(name, data):
    """Mutation acknowledgements omit history still available via read_skill_run."""
    if name in {"begin_skill", "select_skill_run", "finish_skill"}:
        return {**{k: deepcopy(v) for k, v in data.items() if k != "calls"},
                "call_count": len(data["calls"])}
    return data


class ObservedSkills:
    scope_kind = None
    scope_argument = None
    script_files = {}

    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "uniqueItems": True, "maxItems": 100}

        def schema(properties, required=None):
            return {"type": "object", "properties": properties, "required": list(properties) if required is None else required, "additionalProperties": False}

        definitions = {
            "read_task": ("Read the public task and fixed workflow library.", schema({})),
            "read_skill_file": ("Read one public workflow instruction or its optional script.", schema({"skill_id": text, "path": text}, ["skill_id"])),
            "begin_skill": ("Observe the selected workflow without routing or authorizing business operations.", schema({"skill_id": text, self.scope_argument: ids}, ["skill_id"])),
            "select_skill_run": ("Select an existing open workflow run for attribution.", schema({"run_id": text})),
            "finish_skill": ("Record claimed completion and output references; does not evaluate business success.", schema({"run_id": text, "output_ids": ids, "notes": {"type": "string", "maxLength": 4000}}, ["run_id"])),
            "read_skill_run": ("Read an observed run with the full actual tool-outcome history, including after closure.", schema({"run_id": text})),
            "run_skill_script": ("Run one fixed optional script using supplied public JSON, without state access.", schema({"skill_id": text, "inputs": {"type": "object"}})),
        }
        self.metadata.update({n: {"description": d, "input_schema": s} for n, (d, s) in definitions.items()})

    def selected_skill(self, identifier):
        selected = [s for s in self.public["skills"] if s["name"] == identifier]
        self.require(len(selected) == 1, "UNKNOWN_SKILL")
        return selected[0]

    def dispatch(self, name, args):
        if name == "read_task":
            return deepcopy(self.public)
        if name == "read_skill_file":
            skill = self.selected_skill(args["skill_id"])
            path = args.get("path", "SKILL.md")
            self.require(path in skill["files"], "SKILL_FILE_NOT_EXPOSED")
            return {"skill_id": skill["name"], "path": path, "content": (self.task / "public/skills" / skill["name"] / path).read_text()}
        if name == "begin_skill":
            self.selected_skill(args["skill_id"])
            for identifier in args.get(self.scope_argument, []):
                self.owned(self.scope_kind, identifier)
            identifier = self.next_id("skill-run")
            row = {"run_id": identifier, "skill_id": args["skill_id"], self.scope_argument: args.get(self.scope_argument, []),
                "status": "open", "calls": [], "output_ids": [], "notes": ""}
            self.state["skill_runs"][identifier] = row
            self.state["active_skill_run"] = identifier
            return deepcopy(row)
        if name in {"select_skill_run", "finish_skill", "read_skill_run"}:
            self.require(args["run_id"] in self.state["skill_runs"], "SKILL_RUN_NOT_FOUND")
            row = self.state["skill_runs"][args["run_id"]]
            if name != "read_skill_run":
                self.require(row["status"] == "open", "SKILL_RUN_CLOSED")
            if name == "select_skill_run":
                self.state["active_skill_run"] = row["run_id"]
            if name == "finish_skill":
                row.update(status="claimed_complete", output_ids=args.get("output_ids", []), notes=args.get("notes", ""))
                if self.state["active_skill_run"] == row["run_id"]:
                    self.state["active_skill_run"] = None
            return deepcopy(row)
        if name == "run_skill_script":
            check_python_executable(sys.executable)
            self.selected_skill(args["skill_id"])
            self.require(args["skill_id"] in self.script_files, "SKILL_HAS_NO_SCRIPT")
            payload = json.dumps(args["inputs"], allow_nan=False)
            self.require(len(payload.encode()) <= getattr(self, "script_input_limit", 131072), "SCRIPT_INPUT_TOO_LARGE")
            path = self.task / "public/skills" / args["skill_id"] / self.script_files[args["skill_id"]]
            with tempfile.TemporaryDirectory(prefix="internal-mcp-script-") as directory:
                from skillhorizon.experiment.construction_envelope import temporary_environment
                try:
                    result = subprocess.run([sys.executable, "-I", "-S", str(path)], input=payload, text=True, capture_output=True,
                        cwd=directory, env=temporary_environment({"PYTHONIOENCODING": "utf-8"}), timeout=5)
                except subprocess.TimeoutExpired:
                    self.require(False, "SCRIPT_TIMEOUT")
            if result.returncode == 2 and len(result.stdout.encode()) <= 8192:
                try:
                    diagnostic = json.loads(result.stdout)["script_input_error"]
                except (ValueError, KeyError, TypeError):
                    diagnostic = None
                if (isinstance(diagnostic, dict) and set(diagnostic) == {"code", "paths", "missing_source_keys"}
                        and isinstance(diagnostic["code"], str)
                        and all(isinstance(diagnostic[k], list) and len(diagnostic[k]) <= 30
                                and all(isinstance(v, str) for v in diagnostic[k]) for k in ("paths", "missing_source_keys"))):
                    self.require(False, "SCRIPT_INPUT_ERROR:" + json.dumps(diagnostic, sort_keys=True))
            self.require(result.returncode == 0, "SCRIPT_INPUT_OR_EXECUTION_ERROR")
            return json.loads(result.stdout)
        return super().dispatch(name, args)

    def record_call(self, name, arguments, result):
        active = self.state["active_skill_run"]
        if active and name not in {"begin_skill", "select_skill_run", "read_skill_run"}:
            self.state["skill_runs"][active]["calls"].append({"sequence": self.world["sequence"], "tool": name,
                "status": result["status"], "error": result["message"] if result["status"] != "success" else ""})
