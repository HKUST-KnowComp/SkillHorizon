"""Coupled-production operations with controller-owned skill declarations."""

from pathlib import Path

from integrations.internal_mcp.io import read_json
from .coupled_build import FORMAT
from .runtime import SkillProduction
from .portfolio_runtime import PortfolioProduction


PROTOCOL = "skill_declaration_v1"
BOOKKEEPING = frozenset({"skill_runs", "active_skill_run"})
RETIRED_TOOLS = frozenset({"begin_skill", "finish_skill", "select_skill_run", "read_skill_run"})


class DeclarationProduction(PortfolioProduction, SkillProduction):
    def __init__(self, task, runtime):
        public = read_json(Path(task) / "public/task.json")
        world = read_json(Path(runtime) / "world.json")
        if (public.get("format") != FORMAT or world.get("format") != FORMAT
                or public.get("task_id") != world.get("task_id")
                or public.get("runtime_identity") != "task_local_v1"
                or public.get("policy", {}).get("skill_protocol") != PROTOCOL
                or not isinstance(world.get("production"), dict)
                or BOOKKEEPING.intersection(world["production"])):
            raise ValueError("Declaration production requires a clean task-local Coupled V2 world")
        super().__init__(task, runtime)
        for name in RETIRED_TOOLS:
            self.metadata.pop(name, None)

    def dispatch(self, name, arguments):
        self.require(name not in RETIRED_TOOLS, "TOOL_NOT_EXPOSED")
        return super().dispatch(name, arguments)

    def record_call(self, name, arguments, result):
        # Only the controller records skill ownership; business events stay native.
        pass
