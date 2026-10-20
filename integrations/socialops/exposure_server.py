import asyncio
import sys

from integrations.internal_mcp.transport import serve
from .exposure_runtime import ExposureSocial


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.socialops.exposure_server TASK RUNTIME")
    asyncio.run(serve(ExposureSocial(sys.argv[1], sys.argv[2]), "c06-exposure-network"))
