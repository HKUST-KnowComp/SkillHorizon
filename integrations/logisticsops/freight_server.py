import asyncio
from pathlib import Path
import sys

from integrations.internal_mcp.transport import serve
from integrations.internal_mcp.io import read_json
from .declaration_runtime import BOOKKEEPING, PROTOCOL, DeclarationFreight
from .freight_runtime import FreightNetwork


def make_runtime(task, runtime):
    public = read_json(Path(task) / "public/task.json")
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol == PROTOCOL:
        return DeclarationFreight(task, runtime)
    if protocol != "observational_v1":
        raise ValueError("Unsupported Freight skill protocol")
    state = read_json(Path(runtime) / "world.json").get("connection_state", {})
    if not BOOKKEEPING.issubset(state):
        raise ValueError("Observational Freight requires its original skill bookkeeping")
    return FreightNetwork(task, runtime)


if __name__ == "__main__":
    asyncio.run(serve(make_runtime(Path(sys.argv[1]), Path(sys.argv[2])), "c09-freight-network"))
