"""Run-free FieldOps steps; no declaration state or implicit business session."""

from typing import Literal

from .observations import QUERY_TOOLS
from .planning import business_step
from .runtime import OperationsRuntime, operations_tool_entries


class DeclarationOperationsRuntime(OperationsRuntime):
    def reload(self):
        super().reload()
        if (self.project.get("skill_protocol") != "skill_declaration_v1"
                or self.project["domain"] != "fieldops"
                or self.project.get("business_program") != "pressure_branches_v3"
                or "active" in self.project or "next_run" in self.project):
            raise ValueError("Declaration FieldOps requires a fresh run-free pressure_branches_v3 world")

    def public_progress(self):
        progress = super().public_progress()
        del progress["lifecycle"]
        return progress

    @property
    def query_tools(self):
        collections = Literal["artifacts", "devices", "windows", "crews", "parts", "isolations", "notices", "services"]
        return [(name, "Read a public resource or committed business record, including historical records."
                 if name == "read_object" else description, {**fields, "collection": collections})
                for name, description, fields in QUERY_TOOLS]

    def execute_step(self, skill_id, step_id, arguments):
        if skill_id == "plan-isolation":
            return business_step(self, skill_id, step_id, arguments)
        self.require(step_id == "compute", "STEP_ORDER", "This skill uses step compute")
        return self.execute(skill_id, arguments)

    def run_skill_script(self, skill_id, step_id, arguments):
        self.require(skill_id == "test-device", "NO_SCRIPT", "Only test-device has a public FieldOps script")
        self.require(step_id == "compute", "STEP_ORDER", "The measurement script uses step compute")
        return self.execute(skill_id, arguments, use_script=True)

    def _dispatch(self, name, arguments):
        allowed = {"read_project", "read_artifact", "read_skill_file", "search_objects", "read_object",
                   "execute_step", "run_skill_script"}
        self.require(name in allowed, "UNKNOWN_SKILL", "Unknown public business tool")
        self.require(not (name in {"search_objects", "read_object"} and arguments.get("collection") == "active"),
                     "COLLECTION", "Declarations are not business objects")
        return getattr(self, name)(**arguments)

    def tool_entries(self, execution_mode):
        entries = [entry for entry in operations_tool_entries(self, "procedural")
                   if entry[0] in {"read_project", "read_artifact", "read_skill_file", "search_objects", "read_object"}]
        entries[0] = ("read_project", "Inspect the business goal, resources and pending work; recorded handoff is not certification.", {})
        fields = {"skill_id": str, "step_id": str, "arguments": dict}
        return entries + [("execute_step", "Execute a business step with explicit inputs and actual prerequisite artifacts.", fields),
                          ("run_skill_script", "Execute the existing test-device measurement script with explicit inputs.", fields)]
