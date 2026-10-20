"""Run-free DataOps business steps; declaration lifecycle belongs to the gateway."""

from typing import Literal

from .runtime import DataflowRuntime, dataflow_tool_entries
from .skills import PROCEDURES


class DeclarationDataflowRuntime(DataflowRuntime):
    def public_progress(self):
        progress = super().public_progress()
        del progress["lifecycle"]
        return progress

    def read_project(self):
        project = super().read_project()
        del project["collections"]["active"]
        return project

    def _public_collection(self, name):
        if name == "active":
            return {}
        return super()._public_collection(name)

    def execute_step(self, skill_id, step_id, arguments):
        if skill_id in PROCEDURES:
            return self._business_step(skill_id, step_id, arguments)
        self._require(step_id == "compute", "STEP", "This simple skill uses step compute")
        return self.execute(skill_id, arguments)

    def run_skill_script(self, skill_id, step_id, arguments):
        if skill_id in PROCEDURES:
            return self._business_step(skill_id, step_id, arguments, use_script=True)
        self._require(step_id == "compute", "STEP", "This simple skill uses step compute")
        return self.execute(skill_id, arguments, use_script=True)

    def _dispatch(self, name, arguments):
        # An execution allowlist is necessary: the legacy dispatcher accepts macros.
        allowed = {"read_project", "read_table", "read_skill_file", "search_objects", "read_object",
                   "execute_step", "run_skill_script"}
        self._require(name in allowed, "UNKNOWN_SKILL", "Unknown public business tool")
        self._require(not (name in {"search_objects", "read_object"} and arguments.get("collection") == "active"),
                      "COLLECTION", "Declaration runs are not business objects")
        return getattr(self, name)(**arguments)

    def tool_entries(self, execution_mode):
        queries = [entry for entry in dataflow_tool_entries(self, "procedural")
                   if entry[0] in {"read_project", "read_table", "read_skill_file", "search_objects", "read_object"}]
        descriptions = {
            "read_project": "Inspect public goal, business inventory, pending obligations and deliverable freshness. Recorded handoff is not semantic verification.",
            "search_objects": "Search sources, artifacts, work_packages or delivered notices. Literal case-insensitive text over summary fields; filters are exact AND matches. Use text='' and filters=[] to list. Artifact status is current/stale source freshness, not correctness. Stable ID order; follow next_offset, limit 1..200.",
            "read_object": "Read public object metadata and dependencies. Use read_table for contents. Old artifacts remain readable; current means source freshness, not correctness.",
        }
        collections = Literal["sources", "artifacts", "work_packages", "notices"]
        queries = [(name, descriptions.get(name, description),
                    {**fields, "collection": collections} if "collection" in fields else fields)
                   for name, description, fields in queries]
        fields = {"skill_id": str, "step_id": str, "arguments": dict}
        return queries + [
            ("execute_step", "Execute a business step using explicit inputs and real intermediate artifacts.", fields),
            ("run_skill_script", "Execute one optional script step with explicit business inputs.", fields),
        ]
