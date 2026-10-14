"""Minimal MCP stdio server used by the extrovert test suite.

Tools:
- echo(message: str) — returns the message unchanged
- ping() — returns "pong" (zero-argument smoke probe)
- version() — returns a version string (zero-argument smoke probe)

Run: python3 tests/echo_server.py   (speaks MCP over stdio)
"""

from __future__ import annotations

import anyio
from mcp.server.mcpserver import MCPServer

server = MCPServer(name="extrovert-test-echo", version="0.1.0", log_level="WARNING")


@server.tool(description="Echo the message back unchanged.")
def echo(message: str) -> str:
    return message


@server.tool(description="Liveness probe; always answers 'pong'.")
def ping() -> str:
    return "pong"


@server.tool(description="Return the echo server's version.")
def version() -> str:
    return "extrovert-test-echo/0.1.0"


if __name__ == "__main__":
    anyio.run(server.run_stdio_async)
