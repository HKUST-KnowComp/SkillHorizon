"""Observational skills for the source-derived planning business environment."""

from copy import deepcopy
import json
import subprocess
import sys
import tempfile

from .kernel import Planning, require, schema


class SkillPlanning(Planning):
    def __init__(self, task, runtime):
        super().__init__(task, runtime)
        text = {"type": "string", "minLength": 1}
        ids = {"type": "array", "items": text, "maxItems": 64, "uniqueItems": True}
        definitions = {
            "read_task": ("Read public planning obligations and the reusable skill library.", schema({})),
            "read_skill_file": ("Read a public workflow or its optional script.", schema({"skill_id": text, "path": text}, ["skill_id"])),
            "begin_skill": ("Observe the actor's chosen workflow; does not route or authorize business tools.", schema({"skill_id": text, "page_ids": ids}, ["skill_id"])),
            "select_skill_run": ("Attribute subsequent work to an existing open run.", schema({"run_id": text})),
            "finish_skill": ("Record claimed completion, not evaluated business success.", schema({"run_id": text, "output_ids": ids, "notes": {"type": "string", "maxLength": 4000}}, ["run_id"])),
            "read_skill_run": ("Read one observed workflow run and its actual call outcomes.", schema({"run_id": text})),
            "run_skill_script": ("Run the optional reconciliation script on supplied public JSON; cannot read environment state or approve documents.", schema({"skill_id": text, "inputs": {"type": "object"}})),
        }
        self.declaration_only = self.public["policy"]["skill_protocol"] == "skill_declaration_v1"
        if self.declaration_only:
            if "skill_runs" in self.state or "active_skill_run" in self.state:
                raise ValueError("Declaration tasks cannot retain business skill bookkeeping")
            for name in ("begin_skill", "finish_skill", "select_skill_run", "read_skill_run"):
                definitions.pop(name)
        self.metadata.update({name: {"description": description, "input_schema": inputs} for name, (description, inputs) in definitions.items()})

    def skill(self, identifier):
        matches = [s for s in self.public["skills"] if s["name"] == identifier]
        require(len(matches) == 1, "UNKNOWN_SKILL")
        return matches[0]

    def dispatch(self, name, args):
        if name == "read_task":
            return deepcopy(self.public)
        if name == "read_skill_file":
            spec = self.skill(args["skill_id"])
            path = args.get("path", "SKILL.md")
            require(path in spec["files"], "SKILL_FILE_NOT_EXPOSED")
            return {"skill_id": spec["name"], "path": path, "content": (self.task / "public/skills" / spec["name"] / path).read_text()}
        if name == "begin_skill":
            self.skill(args["skill_id"])
            for identifier in args.get("page_ids", []):
                self.owned("page", identifier)
            identifier = self.next_id("skill-run")
            row = {"run_id": identifier, "skill_id": args["skill_id"], "page_ids": args.get("page_ids", []),
                   "status": "open", "calls": [], "output_ids": [], "notes": ""}
            self.state["skill_runs"][identifier] = row
            self.state["active_skill_run"] = identifier
            return deepcopy(row)
        if name in {"select_skill_run", "finish_skill", "read_skill_run"}:
            require(args["run_id"] in self.state["skill_runs"], "SKILL_RUN_NOT_FOUND")
            row = self.state["skill_runs"][args["run_id"]]
            if name != "read_skill_run":
                require(row["status"] == "open", "SKILL_RUN_CLOSED")
            if name == "select_skill_run":
                self.state["active_skill_run"] = row["run_id"]
            if name == "finish_skill":
                row.update({"status": "claimed_complete", "output_ids": args.get("output_ids", []), "notes": args.get("notes", "")})
                if self.state["active_skill_run"] == row["run_id"]:
                    self.state["active_skill_run"] = None
            return deepcopy(row)
        if name == "run_skill_script":
            require(args["skill_id"] == "reconcile-interface-contracts", "SKILL_HAS_NO_SCRIPT")
            payload = json.dumps(args["inputs"], allow_nan=False)
            require(len(payload.encode()) <= 131072, "SCRIPT_INPUT_TOO_LARGE")
            path = self.task / "public/skills/reconcile-interface-contracts/scripts/merge_contracts.py"
            with tempfile.TemporaryDirectory(prefix="c25-merge-") as directory:
                result = subprocess.run([sys.executable, "-I", "-S", str(path)], input=payload, text=True,
                    capture_output=True, cwd=directory, env={"PYTHONIOENCODING": "utf-8"}, timeout=5)
            require(result.returncode == 0, "SCRIPT_INPUT_OR_EXECUTION_ERROR")
            return json.loads(result.stdout)
        return super().dispatch(name, args)

    def record_call(self, name, arguments, result):
        if self.declaration_only:
            return
        active = self.state["active_skill_run"]
        if active and name not in {"begin_skill", "select_skill_run", "read_skill_run"}:
            self.state["skill_runs"][active]["calls"].append({"sequence": self.world["sequence"], "tool": name,
                "status": result["status"], "page_id": arguments.get("page_id") if isinstance(arguments, dict) else None,
                "error": result["message"] if result["status"] != "success" else ""})
