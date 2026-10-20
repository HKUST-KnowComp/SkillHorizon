import asyncio
from pathlib import Path
import sys

from integrations.internal_mcp.transport import serve
from .runtime import SkillCorrespondence


if __name__ == "__main__":
    asyncio.run(serve(SkillCorrespondence(Path(sys.argv[1]), Path(sys.argv[2])), "c01-correspondence"))
