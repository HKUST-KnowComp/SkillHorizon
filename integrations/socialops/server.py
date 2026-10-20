import asyncio
import sys

from integrations.internal_mcp.transport import serve
from .runtime import SkillSocial


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.socialops.server TASK RUNTIME")
    asyncio.run(serve(SkillSocial(sys.argv[1], sys.argv[2]), "c06-social"))
