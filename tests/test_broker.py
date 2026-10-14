"""Tests for the extrovert capability broker.

Covers: registry response parsing (mock data), launch-command resolution,
environment scrubbing, mounting a local echo server, proxying a tool call,
verification, removal, persistence/auto-remount, and a full integration run
against a real extrovert server process (including the tools/list_changed
notification and, when the network allows it, the live MCP Registry).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from extrovert.broker import Broker, sanitize_alias
from extrovert.config import ConfigStore
from extrovert.registry import (
    RegistryError,
    parse_server_list,
    resolve_launch_command,
    search_servers,
)
from extrovert.server import build_server

ECHO_SERVER = str(Path(__file__).parent / "echo_server.py")
ECHO_COMMAND = f"{sys.executable} {ECHO_SERVER}"

# Real response shape captured from GET /v0/servers?search=filesystem (2026-10).
MOCK_REGISTRY_PAYLOAD: dict[str, Any] = {
    "servers": [
        {
            "server": {
                "$schema": "https://static.modelcontextprotocol.io/schemas/2025-09-29/server.schema.json",
                "name": "com.pulsemcp/remote-filesystem",
                "description": "MCP server for remote filesystem operations on cloud storage.",
                "version": "0.1.5",
                "packages": [
                    {
                        "registryType": "npm",
                        "registryBaseUrl": "https://registry.npmjs.org",
                        "identifier": "remote-filesystem-mcp-server",
                        "version": "0.1.5",
                        "runtimeHint": "npx",
                        "transport": {"type": "stdio"},
                        "environmentVariables": [
                            {"name": "GCS_BUCKET", "isRequired": True},
                            {"name": "GCS_PRIVATE_KEY", "isRequired": True, "isSecret": True},
                        ],
                    }
                ],
            },
            "_meta": {
                "io.modelcontextprotocol.registry/official": {
                    "status": "active",
                    "isLatest": True,
                }
            },
        },
        {
            "server": {
                "name": "com.example/remote-only",
                "description": "A server that only offers a streamable-http endpoint.",
                "version": "1.0.0",
                "remotes": [{"type": "streamable-http", "url": "https://example.com/mcp"}],
            },
            "_meta": {},
        },
    ],
    "metadata": {"nextCursor": "com.example/remote-only:1.0.0", "count": 2},
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> ConfigStore:
    return ConfigStore(home=tmp_path / "extrovert-home")


def result_text(result: Any) -> str:
    texts = [b.text for b in (result.content or []) if isinstance(b, TextContent)]
    return "\n".join(texts)


def result_payload(result: Any) -> dict[str, Any]:
    """Extract the broker tool's dict result (structured or JSON text)."""
    data = getattr(result, "structured_content", None)
    if isinstance(data, dict):
        if set(data) == {"result"} and isinstance(data["result"], dict):
            return data["result"]
        return data
    return json.loads(result_text(result))


# -- registry parsing (mock data) ---------------------------------------------


def test_parse_registry_response() -> None:
    entries = parse_server_list(MOCK_REGISTRY_PAYLOAD)
    assert len(entries) == 2
    first = entries[0]
    assert first.name == "com.pulsemcp/remote-filesystem"
    assert first.version == "0.1.5"
    assert first.description.startswith("MCP server for remote filesystem")
    assert first.installable
    assert not first.remote_only
    pkg = first.packages[0]
    assert pkg.registry_type == "npm"
    assert pkg.identifier == "remote-filesystem-mcp-server"
    assert pkg.required_env == ["GCS_BUCKET", "GCS_PRIVATE_KEY"]
    second = entries[1]
    assert not second.installable
    assert second.remote_only


def test_parse_registry_response_tolerates_junk() -> None:
    payload = {"servers": [{"server": {}}, {"nope": True}, None, {"server": {"name": "a/b"}}]}
    entries = parse_server_list(payload)
    assert [e.name for e in entries] == ["a/b"]


def test_resolve_launch_command_npm() -> None:
    entry = parse_server_list(MOCK_REGISTRY_PAYLOAD)[0]
    plan = resolve_launch_command(entry)
    assert plan.registry_type == "npm"
    assert plan.argv == ["npx", "-y", "remote-filesystem-mcp-server@0.1.5"]
    assert plan.missing_required_env == ["GCS_BUCKET", "GCS_PRIVATE_KEY"]


def test_resolve_launch_command_remote_only() -> None:
    entry = parse_server_list(MOCK_REGISTRY_PAYLOAD)[1]
    with pytest.raises(RegistryError, match="remote"):
        resolve_launch_command(entry)


def test_resolve_launch_command_pypi() -> None:
    payload = {
        "servers": [
            {
                "server": {
                    "name": "com.example/py-server",
                    "version": "2.0.0",
                    "packages": [
                        {
                            "registryType": "pypi",
                            "identifier": "example-mcp",
                            "version": "2.0.0",
                            "transport": {"type": "stdio"},
                        }
                    ],
                }
            }
        ]
    }
    entry = parse_server_list(payload)[0]
    plan = resolve_launch_command(entry)
    assert plan.registry_type == "pypi"
    assert plan.argv[0] in ("uvx", "python3")
    assert plan.argv[-1] == "example-mcp"


# -- naming / classification ----------------------------------------------------


def test_sanitize_alias() -> None:
    assert sanitize_alias("remote-filesystem") == "remote-filesystem"
    assert sanitize_alias("weird name/spaces") == "weird_name_spaces"
    assert sanitize_alias("///") == "server"


def test_classify_identifier() -> None:
    assert Broker.classify_identifier("com.pulsemcp/remote-filesystem") == "registry"
    assert Broker.classify_identifier(ECHO_COMMAND) == "local"
    assert Broker.classify_identifier("./echo_server.py") == "local"
    assert Broker.classify_identifier("/abs/path/echo_server.py") == "local"


# -- environment scrubbing -------------------------------------------------------


def test_env_scrubbing_hides_secrets(store: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setenv("MY_SECRET_API_KEY", "supersecret")
    env = store.scrub_env()
    assert env.get("PATH") == "/usr/bin:/bin"
    assert "MY_SECRET_API_KEY" not in env


def test_env_passthrough_opt_in(store: ConfigStore, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MY_SECRET_API_KEY", "supersecret")
    cfg = store.load_config()
    cfg["env_passthrough"] = ["MY_SECRET_API_KEY"]
    store.save_config(cfg)
    env = store.scrub_env()
    assert env.get("MY_SECRET_API_KEY") == "supersecret"


# -- mount / proxy / verify / remove ----------------------------------------------


@pytest.mark.anyio
async def test_mount_local_echo_and_proxy_call(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        added = await broker.add(ECHO_COMMAND)
        assert added["ok"], added
        assert added["alias"] == "echo_server"
        assert added["source"] == "local"
        assert set(added["tools"]) == {
            "echo_server__echo",
            "echo_server__ping",
            "echo_server__version",
        }

        tool_names = {t.name for t in await server.list_tools()}
        assert "echo_server__echo" in tool_names
        assert "capability_search" in tool_names

        result = await server.call_tool("echo_server__echo", {"message": "hello broker"})
        assert not result.is_error
        assert "hello broker" in result_text(result)


@pytest.mark.anyio
async def test_verify_writes_trust_record(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        await broker.add(ECHO_COMMAND)

        report = await broker.verify("echo_server")
        assert report["ok"], report
        assert report["passed"], report
        assert report["probe"] in ("ping", "version")
        assert "ping ok" in report["evidence"]

        trust = store.load_trust()
        assert trust["echo_server"]["passed"] is True
        assert trust["echo_server"]["verified_at"]

        cfg = store.load_config()
        assert cfg["servers"]["echo_server"]["verified"] is True


@pytest.mark.anyio
async def test_remove_unregisters_tools(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        await broker.add(ECHO_COMMAND)
        assert "echo_server" in broker.mounts

        removed = await broker.remove("echo_server")
        assert removed["ok"], removed
        assert "echo_server__echo" in removed["removed_tools"]

        tool_names = {t.name for t in await server.list_tools()}
        assert "echo_server__echo" not in tool_names
        assert "echo_server" not in store.load_config()["servers"]

        # Calling a removed tool is a clean error, not a crash.
        with pytest.raises(ToolError, match="Unknown tool"):
            await server.call_tool("echo_server__echo", {"message": "x"})


@pytest.mark.anyio
async def test_duplicate_alias_rejected(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        first = await broker.add(ECHO_COMMAND)
        assert first["ok"]
        second = await broker.add(ECHO_COMMAND)
        assert not second["ok"]
        assert "already in use" in second["error"]
        aliased = await broker.add(ECHO_COMMAND, alias="echo2")
        assert aliased["ok"], aliased
        assert "echo2__echo" in aliased["tools"]


@pytest.mark.anyio
async def test_remount_verified_on_restart(store: ConfigStore) -> None:
    server1 = build_server(store)
    async with Broker(server1, store) as broker1:
        await broker1.add(ECHO_COMMAND)
        await broker1.verify("echo_server")

    # A fresh server + broker over the same store auto-remounts verified servers.
    server2 = build_server(store)
    async with Broker(server2, store) as broker2:
        summary = await broker2.remount_verified()
        assert summary["remounted"] == ["echo_server"]
        assert summary["failed"] == {}

        result = await server2.call_tool("echo_server__echo", {"message": "back again"})
        assert not result.is_error
        assert "back again" in result_text(result)


@pytest.mark.anyio
async def test_unverified_server_not_remounted(store: ConfigStore) -> None:
    server1 = build_server(store)
    async with Broker(server1, store) as broker1:
        await broker1.add(ECHO_COMMAND)  # added but never verified

    server2 = build_server(store)
    async with Broker(server2, store) as broker2:
        summary = await broker2.remount_verified()
        assert summary["remounted"] == []
        tool_names = {t.name for t in await server2.list_tools()}
        assert "echo_server__echo" not in tool_names


@pytest.mark.anyio
async def test_bad_local_command_fails_cleanly(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        result = await broker.add(f"{sys.executable} /nonexistent/nope_server.py")
        assert not result["ok"]
        assert result["error"]


@pytest.mark.anyio
async def test_audit_log_written(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        await broker.add(ECHO_COMMAND)
        await broker.verify("echo_server")
        await broker.remove("echo_server")

    lines = [json.loads(line) for line in store.log_path.read_text().splitlines() if line.strip()]
    events = [line["event"] for line in lines]
    assert "install" in events
    assert "verify" in events
    assert "remove" in events
    assert all("ts" in line for line in lines)


# -- live registry (skipped when offline) -----------------------------------------


@pytest.mark.anyio
async def test_live_registry_search() -> None:
    try:
        entries = await search_servers("filesystem", limit=3)
    except RegistryError as exc:
        pytest.skip(f"registry unreachable: {exc}")
    assert entries, "registry returned no results"
    for entry in entries:
        assert entry.name and "/" in entry.name
        assert entry.version
    assert any(e.installable or e.remote_only for e in entries)


@pytest.mark.anyio
async def test_dead_downstream_gives_clear_error(store: ConfigStore) -> None:
    server = build_server(store)
    async with Broker(server, store) as broker:
        await broker.add(ECHO_COMMAND)
        # Simulate a downstream crash: terminate the subprocess without
        # unregistering its tools.
        broker.mounts["echo_server"].close_event.set()
        with anyio.move_on_after(10):
            await broker.mounts["echo_server"].closed_event.wait()

        with pytest.raises(ToolError, match="unavailable"):
            await server.call_tool("echo_server__echo", {"message": "x"})

        listing = broker.list_mounts()
        entry = listing["servers"][0]
        assert entry["healthy"] is False
        assert entry["last_error"]


# -- full integration: real server process, real MCP client ------------------------


@pytest.mark.anyio
async def test_integration_full_flow(tmp_path: Path) -> None:
    """Drive a real extrovert server process over stdio end to end."""
    home = tmp_path / "integration-home"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "EXTROVERT_HOME": str(home),
    }
    params = StdioServerParameters(command=sys.executable, args=["-m", "extrovert"], env=env)
    notifications: list[str] = []

    async def on_message(message: Any) -> None:
        notifications.append(str(message))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, message_handler=on_message) as session:
            await session.initialize()

            # 1. only the five broker tools exist initially
            initial = {t.name for t in (await session.list_tools()).tools}
            assert initial == {
                "capability_search",
                "capability_add",
                "capability_verify",
                "capability_list",
                "capability_remove",
            }

            # 2. search the live registry (tolerate offline)
            searched = result_payload(
                await session.call_tool("capability_search", {"query": "filesystem", "limit": 3})
            )
            if searched["ok"]:
                assert searched["count"] > 0
                assert all("name" in r and "packages" in r for r in searched["results"])

            # 3. mount the local echo server
            added = result_payload(
                await session.call_tool("capability_add", {"identifier": ECHO_COMMAND})
            )
            assert added["ok"], added
            alias = added["alias"]
            assert alias == "echo_server"

            # 4. the client was told the tool list changed
            assert any("tools/list_changed" in n for n in notifications), notifications

            # 5. the proxied tool is listed and callable
            after_add = {t.name for t in (await session.list_tools()).tools}
            assert f"{alias}__echo" in after_add
            echoed = await session.call_tool(f"{alias}__echo", {"message": "integration works"})
            assert not echoed.is_error
            assert "integration works" in result_text(echoed)

            # 6. verify
            verified = result_payload(await session.call_tool("capability_verify", {"alias": alias}))
            assert verified["ok"] and verified["passed"], verified

            # 7. list shows the mount with verification status
            listed = result_payload(await session.call_tool("capability_list", {}))
            entry = next(s for s in listed["servers"] if s["alias"] == alias)
            assert entry["source"] == "local"
            assert entry["healthy"] is True
            assert entry["verified"] is True
            assert f"{alias}__echo" in entry["tools"]

            # 8. remove; tools disappear and the client is told again
            removed = result_payload(await session.call_tool("capability_remove", {"alias": alias}))
            assert removed["ok"], removed
            after_remove = {t.name for t in (await session.list_tools()).tools}
            assert f"{alias}__echo" not in after_remove
            assert sum("tools/list_changed" in n for n in notifications) >= 2

    # 9. state files were written under EXTROVERT_HOME
    assert (home / "config.json").exists()
    assert json.loads((home / "trust.json").read_text())  # verify record kept as history
    assert (home / "broker.log").exists()
