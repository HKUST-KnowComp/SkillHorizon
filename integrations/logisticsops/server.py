"""MCP entry point for connected airline skill tasks."""

import argparse
import asyncio
from pathlib import Path

from integrations.internal_mcp.transport import serve
from .runtime import Connections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("task", type=Path)
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(Connections(args.task, args.runtime), "SkillHorizon C09 connection recovery"))


if __name__ == "__main__":
    main()
