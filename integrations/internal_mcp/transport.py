"""Real MCP stdio transport for a synchronous domain runtime."""

import json

from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server


async def serve(runtime, name):
    async def listing(context, params):
        return types.ListToolsResult(tools=[types.Tool(name=key, description=value["description"],
            inputSchema=value["input_schema"]) for key, value in runtime.metadata.items()])

    async def calling(context, params):
        result = runtime.call(params.name, params.arguments or {})
        if hasattr(runtime, "is_error"):
            is_error = runtime.is_error(params.name, result)
        else:
            is_error = not result["ok"] if params.name in getattr(runtime, "workspace_names", ()) else result["status"] != "success"
        return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(result, allow_nan=False))],
            structuredContent=result, isError=is_error)

    server = Server(name, on_list_tools=listing, on_call_tool=calling)
    async with stdio_server() as (reader, writer):
        await server.run(reader, writer, server.create_initialization_options())
