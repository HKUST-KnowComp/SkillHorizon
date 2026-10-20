"""One CLI, also usable through submission/run.py native <arguments>."""
import argparse
import asyncio
import json
from pathlib import Path

from . import AGENTS, prepare, run


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare", help="Export one existing submission bundle; no API or credential access")
    p.add_argument("bundle", type=Path)
    p.add_argument("output", type=Path)
    p.add_argument("--agent", choices=AGENTS, required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--max-calls", type=int, required=True)
    p.add_argument("--agent-version", help="Required exact CLI version for codex/claude-code")
    p = sub.add_parser("run", help="Explicit native provider execution; requires a running Docker daemon")
    p.add_argument("prepared", type=Path)
    p.add_argument("--credential-env", required=True)
    p.add_argument("--api-base", help="HTTPS native API origin or /v1 base; no credentials in URL")
    p = sub.add_parser("_worker", help=argparse.SUPPRESS)
    p.add_argument("prepared", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.bundle, args.output, agent=args.agent, model=args.model,
                             max_calls=args.max_calls, agent_version=args.agent_version)
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "run":
            return run(args.prepared, credential_env=args.credential_env, api_base=args.api_base)
        from .runner import execute
        return asyncio.run(execute(args.prepared))
    except (ValueError, RuntimeError) as exc:
        parser.exit(2, str(exc) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
