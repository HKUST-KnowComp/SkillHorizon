"""Freight V2 business execution with controller-owned declarations."""

from pathlib import Path

from integrations.internal_mcp.io import read_json
from .freight_build import FORMAT
from .freight_runtime import FreightNetwork
from .kernel import require


PROTOCOL = "skill_declaration_v1"
BOOKKEEPING = frozenset({"skill_runs", "active_skill_run", "next_skill_run"})
RETIRED_TOOLS = frozenset({"begin_skill", "select_skill_run", "finish_skill", "read_skill_run"})


def declaration_state_clean(world):
    state, network = world.get("connection_state"), world.get("transfer_network")
    if not isinstance(state, dict) or not isinstance(network, dict) or BOOKKEEPING.intersection(state):
        return False
    records = [state.get("clearances"), *(network.get(name) for name in ("releases", "trips", "forwardings"))]
    return all(isinstance(rows, dict) and all(isinstance(row, dict) and "produced_by_skill_run" not in row
                                             for row in rows.values()) for rows in records)


class DeclarationFreight(FreightNetwork):
    def __init__(self, task, runtime):
        public = read_json(Path(task) / "public/task.json")
        world = read_json(Path(runtime) / "world.json")
        require(public.get("format") == world.get("format") == FORMAT,
                "DECLARATION_FREIGHT_FORMAT")
        require(public.get("policy", {}).get("skill_protocol") == PROTOCOL,
                "DECLARATION_PROTOCOL_REQUIRED")
        require(public.get("runtime_identity") == "task_local_v1",
                "DECLARATION_RUNTIME_IDENTITY")
        require(declaration_state_clean(world), "DECLARATION_LEGACY_STATE")
        super().__init__(task, runtime)
        for name in RETIRED_TOOLS:
            self.metadata.pop(name, None)

    def dispatch(self, name, args):
        require(name not in RETIRED_TOOLS, "TOOL_NOT_EXPOSED")
        return super().dispatch(name, args)

    def attribution_fields(self):
        return {}

    def record_call(self, name, arguments, result):
        # The controller owns attribution; domain records retain business lineage.
        pass
