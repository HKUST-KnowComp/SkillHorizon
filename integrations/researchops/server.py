import asyncio
from pathlib import Path
import sys

from integrations.internal_mcp.transport import serve
from integrations.internal_mcp.io import read_json
from .declaration_runtime import BOOKKEEPING, PROTOCOL, DeclarationLibrary
from .runtime import SkillLibrary


def make_runtime(task, runtime):
    public = read_json(Path(task) / "public/task.json")
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol == PROTOCOL:
        return DeclarationLibrary(task, runtime)
    if protocol != "observational_v1" or not BOOKKEEPING.issubset(
            read_json(Path(runtime) / "world.json").get("library", {})):
        raise ValueError("Observational library requires its original skill bookkeeping")
    return SkillLibrary(task, runtime)


if __name__ == "__main__":
    asyncio.run(serve(make_runtime(Path(sys.argv[1]), Path(sys.argv[2])), "c11-library"))
