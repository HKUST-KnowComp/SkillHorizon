import asyncio
import sys

from integrations.internal_mcp.transport import serve
from .runtime import SkillFinOps


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.c47_finops.server TASK RUNTIME")
    asyncio.run(serve(SkillFinOps(sys.argv[1], sys.argv[2]), "c47-finops"))
