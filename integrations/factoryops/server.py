import asyncio
from pathlib import Path
import sys

from integrations.internal_mcp.transport import serve
from integrations.internal_mcp.io import read_json
from .declaration_runtime import BOOKKEEPING, PROTOCOL, DeclarationProduction
from .runtime import SkillProduction


def make_runtime(task, runtime):
    public = read_json(Path(task) / "public/task.json")
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol == PROTOCOL:
        return DeclarationProduction(task, runtime)
    if protocol != "observational_v1" or not BOOKKEEPING.issubset(
            read_json(Path(runtime) / "world.json").get("production", {})):
        raise ValueError("Observational production requires its original skill bookkeeping")
    return SkillProduction(task, runtime)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.factoryops.server TASK RUNTIME")
    asyncio.run(serve(make_runtime(sys.argv[1], sys.argv[2]), "c50-production"))
