import asyncio
from pathlib import Path
import sys

from integrations.internal_mcp.transport import serve
from .network_runtime import TransferNetwork


if __name__ == "__main__":
    asyncio.run(serve(TransferNetwork(Path(sys.argv[1]), Path(sys.argv[2])), "c09-transfer-network"))
