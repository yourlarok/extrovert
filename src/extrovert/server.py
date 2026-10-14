"""The extrovert MCP server: exposes the capability_* tools over stdio."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from mcp.server.mcpserver import Context, MCPServer

from . import __version__
from .broker import Broker
from .config import ConfigStore
from .registry import RegistryError, search_servers

INSTRUCTIONS = """\
extrovert is a capability broker. When planning or executing a task and one
step is a generic external capability (image/video/audio generation, OCR or
document parsing, web scraping or browser control, translation, SaaS or
vendor APIs, 3D rendering, format conversion, ...), do not build it from
scratch — find an existing capability first:

0. capability_consider(task) — planning-phase advice: is there an existing
   capability for this subtask? (cached, cheap; call before implementing)
1. capability_search(query) — search the official MCP Registry for servers
2. capability_add(identifier, alias?) — install & mount one (registry server
   name like 'com.example/foo', or a local command like
   'python3 /path/to/server.py'); its tools appear as '<alias>__<tool>'
3. capability_verify(alias) — smoke-test the mounted server
4. capability_list() — show mounted servers, tools and verification status
5. capability_remove(alias) — unmount and terminate the server

Mounted tools become callable immediately after capability_add returns.
"""

_BROKER_TOOL_NAMES = (
    "capability_search",
    "capability_consider",
    "capability_add",
    "capability_verify",
    "capability_list",
    "capability_remove",
)


def build_server(store: ConfigStore | None = None) -> MCPServer:
    store = store or ConfigStore()
    state: dict[str, Broker] = {}

    @asynccontextmanager
    async def lifespan(server: MCPServer):
        async with Broker(server, store) as broker:
            state["broker"] = broker
            try:
                await broker.remount_verified()
                yield None
            finally:
                state.pop("broker", None)

    server = MCPServer(
        name="extrovert",
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
        log_level="WARNING",
        warn_on_duplicate_tools=False,
    )

    def broker() -> Broker:
        try:
            return state["broker"]
        except KeyError:
            raise RuntimeError("extrovert server is not fully started yet") from None

    @server.tool(
        name="capability_search",
        description=(
            "Search the official MCP Registry for installable MCP servers. "
            "Returns structured candidates with name, description, version and "
            "packages (npm/pypi identifiers). Pass a server's name to "
            "capability_add to install it."
        ),
    )
    async def capability_search(query: str, limit: int = 5) -> dict[str, Any]:
        try:
            entries = await search_servers(query, limit=limit)
        except RegistryError as exc:
            return {"ok": False, "error": str(exc)}
        return {
            "ok": True,
            "query": query,
            "count": len(entries),
            "results": [e.to_dict() for e in entries],
            "hint": "install one with capability_add('<name>'), e.g. capability_add('com.example/foo')",
        }

    @server.tool(
        name="capability_consider",
        description=(
            "Planning-phase advice: given a subtask description, suggest "
            "existing capabilities from the official MCP Registry that could "
            "do it (with local verification status). Call while decomposing a "
            "task, before implementing anything yourself. Results are cached "
            "for 10 minutes; registry outages degrade to 'implement it "
            "yourself' advice. Use capability_search for raw candidate lists."
        ),
    )
    async def capability_consider(task: str, limit: int = 5) -> dict[str, Any]:
        return await broker().consider(task, limit=limit)

    @server.tool(
        name="capability_add",
        description=(
            "Install and mount an MCP server. identifier is either a registry "
            "server name (e.g. 'com.example/foo', see capability_search) or a "
            "local stdio command (e.g. 'python3 /path/to/server.py'). The "
            "server's tools become available immediately as '<alias>__<tool>'."
        ),
    )
    async def capability_add(identifier: str, alias: str | None = None, *, ctx: Context) -> dict[str, Any]:
        return await broker().add(identifier, alias=alias, ctx=ctx)

    @server.tool(
        name="capability_verify",
        description=(
            "Smoke-test a mounted server: ping, list_tools, and one safe "
            "zero-argument probe call (echo/ping/hello/version) when available. "
            "The result is recorded in the local trust store. This is a liveness "
            "check, not a security audit."
        ),
    )
    async def capability_verify(alias: str) -> dict[str, Any]:
        return await broker().verify(alias)

    @server.tool(
        name="capability_list",
        description=(
            "List mounted capability servers with their tools, source, health "
            "and verification status."
        ),
    )
    async def capability_list() -> dict[str, Any]:
        return broker().list_mounts()

    @server.tool(
        name="capability_remove",
        description=(
            "Unmount a capability server: terminate its subprocess, unregister "
            "its '<alias>__<tool>' tools and remove it from the persisted config."
        ),
    )
    async def capability_remove(alias: str, *, ctx: Context) -> dict[str, Any]:
        return await broker().remove(alias, ctx=ctx)

    return server


async def serve() -> None:
    await build_server().run_stdio_async()


def broker_tool_names() -> tuple[str, ...]:
    return _BROKER_TOOL_NAMES
