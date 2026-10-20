import asyncio
import sys

from integrations.internal_mcp.transport import serve
from .workload_runtime import WorkloadFinOps


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m integrations.c47_finops.workload_server TASK RUNTIME")
    asyncio.run(serve(WorkloadFinOps(sys.argv[1], sys.argv[2]), "c47-workload-network"))
