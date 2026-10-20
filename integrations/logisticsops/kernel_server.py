"""Kernel qualification server. Full task/skill server is a separate stage."""

import argparse
import asyncio
from pathlib import Path

from integrations.internal_mcp.transport import serve
from .kernel import Airline


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("runtime", type=Path)
    args = parser.parse_args()
    asyncio.run(serve(Airline(args.source, args.runtime), "SkillHorizon C09 airline kernel"))


if __name__ == "__main__":
    main()
