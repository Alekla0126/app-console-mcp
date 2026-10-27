"""Expose a Toolset as an MCP server over stdio (works with mcp 1.x FastMCP and 2.x MCPServer)."""
from __future__ import annotations

import functools

from .core import Toolset


def build(toolset: Toolset):
    try:
        from mcp.server.mcpserver import MCPServer as Server
        from mcp.server.mcpserver.exceptions import ToolError
    except ImportError:  # mcp < 2
        from mcp.server.fastmcp import FastMCP as Server
        from mcp.server.fastmcp.exceptions import ToolError
    from mcp.types import ToolAnnotations

    server = Server(toolset.server_name, instructions=toolset.instructions)
    for fn in toolset.tools:
        server.add_tool(
            _plain_errors(fn, ToolError),
            name=fn.__name__,
            annotations=ToolAnnotations(readOnlyHint=not fn.writes, destructiveHint=fn.writes,
                                        idempotentHint=not fn.writes, openWorldHint=True),
            structured_output=False,  # plain text only: no duplicated JSON copy of every answer
        )
    return server


def _plain_errors(fn, tool_error):
    """Turn any failure into a ToolError so the model reads the message (the SDK hides other exceptions)."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except Exception as err:
            raise tool_error(str(err) or type(err).__name__) from None
    return wrapper


def serve(toolset: Toolset) -> None:
    build(toolset).run()
