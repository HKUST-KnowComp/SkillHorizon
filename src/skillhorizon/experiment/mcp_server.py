from skillhorizon.host_runtime import require_supported_python

require_supported_python()

import argparse
import asyncio
from pathlib import Path
import shutil
import os
import sys

from .project_runtime import project_runtime
from .tasks import load_task


def main() -> None:
    require_supported_python()
    parser = argparse.ArgumentParser(
        description="Serve a SkillHorizon task via MCP stdio."
    )
    parser.add_argument("task", type=Path)
    parser.add_argument("runtime", type=Path)
    parser.add_argument("--bundle", action="store_true")
    args = parser.parse_args()
    from .execution_bundle import ensure_execution_known, ExecutionGuardedBusiness, ProductionExecution
    ensure_execution_known(args.task)
    from ..util import load_json
    task = load_json(args.task / "private_task.json") if args.bundle else load_task(args.task)
    if args.bundle:
        from .business_binding import business_binding
        if business_binding(args.task)["business_transport"] == "managed_worker":
            from .managed_session import serve_managed
            asyncio.run(serve_managed(args.task, args.runtime))
            return
    from .internal_mcp import FORMAT as INTERNAL_FORMAT, ROOT
    if task["format"] == INTERNAL_FORMAT:
        paths = [str(args.task.resolve()), str(args.runtime.resolve())]
        os.chdir(ROOT)
        os.execv(sys.executable, [sys.executable, "-m", "integrations.internal_mcp.framework", "serve", *paths])
    if not args.bundle and args.runtime.resolve().is_relative_to(args.task.resolve()):
        raise SystemExit("Runtime must be outside the source task")
    if args.runtime.exists():
        raise SystemExit("Runtime must be fresh")
    shutil.copytree(args.task / "checkpoint", args.runtime)
    workspace = None
    if args.bundle:
        from .workspace_bundle import start_workspace
        from .workspace import workspace_entries
        workspace = start_workspace(args.task)
    from mcp.server import MCPServer
    from ..public_contract import register_tools
    business = project_runtime(task, args.runtime)
    server = MCPServer("SkillHorizon Project")
    register_tools(server, ExecutionGuardedBusiness(args.task, business), business.tool_entries(task["execution_mode"]))
    if args.bundle:
        if workspace is not None:
            register_tools(server, workspace, workspace_entries())
            from .execution_bundle import ProductionExecution
            from .python_execution import python_entries
            if isinstance(workspace, ProductionExecution):
                register_tools(server, workspace, python_entries())
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
