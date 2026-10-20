"""Launch one isolated C25 real-MCP worker."""

import asyncio
import sys

from integrations.internal_mcp.transport import serve
from .runtime import SkillPlanning


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.docops.server TASK RUNTIME")
    asyncio.run(serve(SkillPlanning(sys.argv[1], sys.argv[2]), "c25-planning"))
