import asyncio
import sys

from integrations.internal_mcp.transport import serve
from integrations.internal_mcp.io import read_json
from pathlib import Path
from .declaration_runtime import DeclarationReachSocial, PROTOCOL
from .reach_runtime import ReachSocial


def make_runtime(task, runtime):
    public = read_json(Path(task) / "public/task.json")
    protocol = public.get("policy", {}).get("skill_protocol", "observational_v1")
    if protocol == PROTOCOL:
        return DeclarationReachSocial(task, runtime)
    if protocol != "observational_v1":
        raise ValueError("Unsupported Reach skill protocol")
    state = read_json(Path(runtime) / "world.json").get("social", {})
    if "skill_runs" not in state or "active_skill_run" not in state:
        raise ValueError("Observational Reach requires its original skill bookkeeping")
    return ReachSocial(task, runtime)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.socialops.reach_server TASK RUNTIME")
    asyncio.run(serve(make_runtime(sys.argv[1], sys.argv[2]), "c06-reach"))
