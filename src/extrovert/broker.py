"""The capability broker core.

A Broker owns downstream MCP server mounts: it spawns each server as a
stdio subprocess with a scrubbed environment, performs the MCP handshake,
registers the server's tools on the parent MCPServer under the
``<alias>__<tool>`` namespace, forwards calls, verifies servers with
smoke probes, and persists mount/verify state through a ConfigStore.
"""

from __future__ import annotations

import inspect
import keyword
import os
import re
import shlex
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import anyio
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import Tool as MCPTool

from .config import ConfigStore, utc_now_iso
from .registry import (
    RegistryError,
    get_server,
    resolve_launch_command,
    search_servers,
)

MOUNT_TIMEOUT_SECONDS = float(os.environ.get("EXTROVERT_MOUNT_TIMEOUT", "90"))
CONSIDER_CACHE_TTL_SECONDS = 600  # planning-phase cache for capability_consider
MAX_TOOL_NAME_LENGTH = 128  # SEP-986 tool name limit
PROBE_TOOL_PATTERN = re.compile(r"echo|ping|hello|version", re.IGNORECASE)
REGISTRY_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]*/[A-Za-z0-9._-]+$")
LOCAL_LAUNCHERS = {"python", "python3", "node", "npx", "uvx", "uv", "deno", "bun"}
SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs", ".ts")

_JSON_TYPE_MAP: dict[str, type] = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": bool,
    "array": list,
    "object": dict,
}


class BrokerError(Exception):
    """A mount/unmount operation failed in an expected, reportable way."""


def sanitize_alias(name: str) -> str:
    alias = re.sub(r"[^A-Za-z0-9._-]", "_", name.strip())
    alias = alias.strip("._") or "server"
    return alias[:48]


def prefixed_tool_name(alias: str, tool_name: str) -> str:
    return f"{alias}__{tool_name}"


def _safe_param_name(raw: str, used: set[str]) -> str:
    if raw.isidentifier() and not keyword.iskeyword(raw) and raw not in used:
        return raw
    candidate = re.sub(r"\W", "_", raw)
    if not candidate or candidate[0].isdigit():
        candidate = "p_" + candidate
    if keyword.iskeyword(candidate):
        candidate += "_"
    while candidate in used:
        candidate += "_"
    return candidate


def _json_type_to_python(prop: Any) -> Any:
    if isinstance(prop, dict):
        jtype = prop.get("type")
        if isinstance(jtype, list):
            jtype = next((t for t in jtype if t != "null"), None)
        if isinstance(jtype, str) and jtype in _JSON_TYPE_MAP:
            return _JSON_TYPE_MAP[jtype]
    return Any


def _synthesize_signature(input_schema: Any) -> tuple[inspect.Signature, dict[str, str], set[str]]:
    """Build a Python signature from a downstream tool's JSON input schema.

    Returns (signature, rename_map, optional_params). ``rename_map`` maps the
    synthesized Python parameter name back to the original argument name when
    the original was not a usable identifier. ``optional_params`` are the
    parameters that were not required downstream; ``None`` values for those
    are dropped before forwarding so unset options never reach the downstream
    server as explicit JSON nulls.
    """
    params: list[inspect.Parameter] = []
    rename: dict[str, str] = {}
    optional: set[str] = set()
    if isinstance(input_schema, dict):
        props = input_schema.get("properties") or {}
        required = set(input_schema.get("required") or [])
        used: set[str] = set()
        for arg_name, prop in props.items():
            pname = _safe_param_name(str(arg_name), used)
            used.add(pname)
            if pname != arg_name:
                rename[pname] = str(arg_name)
            annotation = _json_type_to_python(prop)
            if arg_name in required:
                default = inspect.Parameter.empty
            else:
                annotation = Optional[annotation]
                default = None
                optional.add(pname)
            params.append(
                inspect.Parameter(
                    pname,
                    kind=inspect.Parameter.KEYWORD_ONLY,
                    default=default,
                    annotation=annotation,
                )
            )
    return inspect.Signature(parameters=params), rename, optional


@dataclass
class Mount:
    alias: str
    source: str  # "registry" | "local"
    identifier: str
    argv: list[str]
    session: ClientSession
    tools: dict[str, MCPTool]
    close_event: anyio.Event
    closed_event: anyio.Event
    mounted_at: str
    registered_names: list[str] = field(default_factory=list)
    healthy: bool = True
    last_error: str | None = None


class Broker:
    """Owns downstream mounts; must be used as an async context manager.

    Anyio task groups and cancel scopes are task-bound, so each mount's
    subprocess and session are entered and exited inside a dedicated owner
    task spawned from the broker's own task group. Consequently the broker
    itself must be entered and exited from the same task (the server
    lifespan guarantees this), while ``add``/``remove``/``call`` may be
    invoked from any request task.
    """

    def __init__(self, server: MCPServer, store: ConfigStore) -> None:
        self.server = server
        self.store = store
        self.mounts: dict[str, Mount] = {}
        self._lock = anyio.Lock()
        self._tg: anyio.abc.TaskGroup | None = None
        self._consider_cache: dict[str, tuple[float, dict[str, Any]]] = {}

    async def __aenter__(self) -> "Broker":
        self._tg = anyio.create_task_group()
        await self._tg.__aenter__()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._close_all_mounts()
        if self._tg is not None:
            self._tg.cancel_scope.cancel()
            await self._tg.__aexit__(None, None, None)
            self._tg = None

    # -- identifier classification -------------------------------------------

    @staticmethod
    def classify_identifier(identifier: str) -> str:
        """Decide whether ``identifier`` is a local command or a registry name."""
        tokens = shlex.split(identifier)
        if not tokens:
            raise BrokerError("empty identifier")
        if len(tokens) > 1:
            return "local"
        head = tokens[0]
        if REGISTRY_NAME_PATTERN.match(head):
            return "registry"
        base = os.path.basename(head)
        if (
            os.sep in head
            or (os.altsep and os.altsep in head)
            or base in LOCAL_LAUNCHERS
            or base.endswith(SCRIPT_SUFFIXES)
        ):
            return "local"
        return "registry"

    @staticmethod
    def _derive_alias(identifier: str, source: str) -> str:
        if source == "registry":
            tail = identifier.rstrip("/").rsplit("/", 1)[-1]
            return sanitize_alias(tail)
        tokens = shlex.split(identifier)
        for token in reversed(tokens):
            base = os.path.basename(token)
            if base.endswith(SCRIPT_SUFFIXES):
                return sanitize_alias(os.path.splitext(base)[0])
        return sanitize_alias(os.path.basename(tokens[0]))

    # -- mounting ---------------------------------------------------------------

    async def add(
        self,
        identifier: str,
        alias: str | None = None,
        ctx: Any = None,
    ) -> dict[str, Any]:
        """Install and mount a capability from the registry or a local command."""
        async with self._lock:
            try:
                source = self.classify_identifier(identifier)
            except BrokerError as exc:
                return {"ok": False, "error": str(exc)}

            warnings: list[str] = []
            if source == "registry":
                try:
                    entry = await get_server(identifier)
                    plan = resolve_launch_command(entry)
                except RegistryError as exc:
                    suggestions = await self._suggest(identifier)
                    result: dict[str, Any] = {"ok": False, "error": str(exc)}
                    if suggestions:
                        result["suggestions"] = suggestions
                    return result
                argv = plan.argv
                if plan.missing_required_env:
                    warnings.append(
                        "package declares required env vars not present in the "
                        f"parent environment: {', '.join(plan.missing_required_env)}; "
                        "the server may fail or misbehave. Add names to "
                        "env_passthrough in ~/.extrovert/config.json to forward them."
                    )
                default_alias = sanitize_alias(entry.name.rsplit("/", 1)[-1])
            else:
                argv = shlex.split(identifier)
                if not argv:
                    return {"ok": False, "error": "empty command"}
                default_alias = self._derive_alias(identifier, "local")

            alias = sanitize_alias(alias) if alias else default_alias
            if alias in self.mounts or alias in self.store.load_config()["servers"]:
                return {
                    "ok": False,
                    "error": f"alias {alias!r} is already in use; "
                    f"call capability_remove({alias!r}) first or pass a different alias",
                }

            try:
                mount = await self._spawn(alias, argv, source=source, identifier=identifier)
            except BrokerError as exc:
                self.store.audit("install_failed", alias=alias, identifier=identifier, error=str(exc))
                return {"ok": False, "error": str(exc), "command": " ".join(argv)}

            if not mount.tools:
                mount.close_event.set()
                return {
                    "ok": False,
                    "error": f"server at {identifier!r} started but exposes no tools; not mounted",
                }

            self.mounts[alias] = mount
            mount.registered_names = self._register_mount_tools(mount)
            self.store.record_server(
                {
                    "alias": alias,
                    "source": source,
                    "identifier": identifier,
                    "command": argv,
                    "tools": sorted(mount.tools),
                    "added_at": mount.mounted_at,
                    "verified": False,
                }
            )
            self.store.audit(
                "install",
                alias=alias,
                source=source,
                identifier=identifier,
                command=" ".join(argv),
                tools=sorted(mount.tools),
            )
            await self._notify_tools_changed(ctx)
            return {
                "ok": True,
                "alias": alias,
                "source": source,
                "identifier": identifier,
                "command": " ".join(argv),
                "tools": mount.registered_names,
                "warnings": warnings,
                "hint": f"tools are live as '{alias}__<tool>'; run capability_verify({alias!r}) to smoke-test the server",
            }

    async def _suggest(self, query: str) -> list[dict[str, Any]]:
        """Best-effort search suggestions when an exact registry lookup fails."""
        term = query.rsplit("/", 1)[-1]
        try:
            entries = await search_servers(term, limit=5)
        except RegistryError:
            return []
        return [e.to_dict() for e in entries]

    # -- planning-phase advice --------------------------------------------------

    async def consider(self, task: str, limit: int = 5) -> dict[str, Any]:
        """Planning-phase advice: which existing capabilities fit ``task``.

        Unlike capability_search (a raw candidate list), this returns advisory
        wording meant to be read while planning, annotates each candidate with
        its local verification history from the trust store, and caches results
        in memory for CONSIDER_CACHE_TTL_SECONDS so planning never hammers the
        registry. Registry outages degrade to "implement it yourself" advice
        with the reason attached.
        """
        key = task.strip().lower()
        now = time.monotonic()
        cached = self._consider_cache.get(key)
        if cached and now - cached[0] < CONSIDER_CACHE_TTL_SECONDS:
            return {**cached[1], "cached": True}

        try:
            entries = await search_servers(task, limit=limit)
        except RegistryError as exc:
            result: dict[str, Any] = {
                "ok": True,
                "task": task,
                "registry_reachable": False,
                "candidates": [],
                "reason": str(exc),
                "advice": "MCP Registry 暂不可达，该子任务先自行实现；网络恢复后可重新 capability_consider。",
            }
            self._consider_cache[key] = (now, result)
            return result

        trust = self.store.load_trust()
        candidates: list[dict[str, Any]] = []
        skipped = 0
        for entry in entries:
            if not entry.installable:
                skipped += 1
                continue
            alias = sanitize_alias(entry.name.rsplit("/", 1)[-1])
            record = trust.get(alias) or {}
            verified = bool(record.get("passed"))
            candidates.append(
                {
                    "name": entry.name,
                    "description": entry.description,
                    "version": entry.version,
                    "source": "mcp-registry",
                    "alias": alias,
                    "verified_locally": verified,
                    "verified_at": record.get("verified_at") if verified else None,
                    "trust_hint": (
                        f"本机已于 {record.get('verified_at')} 验证通过"
                        if verified
                        else "本机尚未验证过；安装后请 capability_verify"
                    ),
                }
            )

        if candidates:
            top = candidates[0]
            advice = (
                f"该子任务可考虑外部能力：{top['name']}（{top['description'][:60]}）。"
                f"安装前请告知用户将运行的命令，然后 capability_add({top['name']!r}) + capability_verify。"
            )
        else:
            advice = "注册表中暂无合适的现成能力，请自行实现该子任务（已确认无现成能力）。"

        result = {
            "ok": True,
            "task": task,
            "registry_reachable": True,
            "candidates": candidates,
            "skipped_remote_only": skipped,
            "advice": advice,
            "note": "capability_verify 是活性检查而非安全审计；只安装你信任的发布者",
        }
        self._consider_cache[key] = (now, result)
        return result

    async def _spawn(self, alias: str, argv: list[str], *, source: str, identifier: str) -> Mount:
        if self._tg is None:
            raise BrokerError("broker is not started; use 'async with Broker(...)'")
        ready_event = anyio.Event()
        close_event = anyio.Event()
        closed_event = anyio.Event()
        outcome: dict[str, Any] = {}
        errlog_path = self.store.logs_dir / f"{alias}.stderr.log"
        env = self.store.scrub_env()

        async def owner() -> None:
            # All subprocess/session contexts are entered and exited inside this
            # one task, satisfying anyio's same-task rule for task groups.
            try:
                async with AsyncExitStack() as stack:
                    self.store.logs_dir.mkdir(parents=True, exist_ok=True)
                    errlog = stack.enter_context(
                        errlog_path.open("a", encoding="utf-8", errors="replace")
                    )
                    params = StdioServerParameters(command=argv[0], args=argv[1:], env=env)
                    read, write = await stack.enter_async_context(
                        stdio_client(params, errlog=errlog)
                    )
                    session = await stack.enter_async_context(ClientSession(read, write))
                    with anyio.fail_after(MOUNT_TIMEOUT_SECONDS):
                        await session.initialize()
                        listed = await session.list_tools()
                    outcome["session"] = session
                    outcome["tools"] = {t.name: t for t in listed.tools}
                    ready_event.set()
                    await close_event.wait()
            except BaseException as exc:
                outcome.setdefault("error", exc)
            finally:
                ready_event.set()
                closed_event.set()

        self._tg.start_soon(owner)
        with anyio.fail_after(MOUNT_TIMEOUT_SECONDS + 10):
            await ready_event.wait()

        if "session" not in outcome:
            close_event.set()
            error = outcome.get("error")
            if isinstance(error, TimeoutError):
                raise BrokerError(
                    f"timed out after {MOUNT_TIMEOUT_SECONDS:.0f}s waiting for {identifier!r} "
                    f"(command: {' '.join(argv)}); see {errlog_path}"
                )
            raise BrokerError(
                f"failed to launch {identifier!r} (command: {' '.join(argv)}): "
                f"{error or 'unknown error'}. See {errlog_path}"
            )

        return Mount(
            alias=alias,
            source=source,
            identifier=identifier,
            argv=argv,
            session=outcome["session"],
            tools=outcome["tools"],
            close_event=close_event,
            closed_event=closed_event,
            mounted_at=utc_now_iso(),
        )

    def _register_mount_tools(self, mount: Mount) -> list[str]:
        registered: list[str] = []
        for tool in mount.tools.values():
            pname = prefixed_tool_name(mount.alias, tool.name)
            if len(pname) > MAX_TOOL_NAME_LENGTH:
                self.store.audit(
                    "tool_skipped", alias=mount.alias, tool=tool.name, reason="prefixed name too long"
                )
                continue
            proxy = self._make_proxy(mount.alias, tool)
            description = (
                f"[proxied from MCP server '{mount.alias}' ({mount.identifier})] "
                f"{tool.description or '(no description)'}"
            )
            self.server.add_tool(proxy, name=pname, description=description)
            registered.append(pname)
        return registered

    def _make_proxy(self, alias: str, tool: MCPTool):
        signature, rename, optional = _synthesize_signature(tool.input_schema)
        orig_name = tool.name

        async def _proxy(**kwargs: Any) -> Any:
            forwarded = {
                rename.get(k, k): v for k, v in kwargs.items() if not (v is None and k in optional)
            }
            return await self._call_downstream(alias, orig_name, forwarded)

        _proxy.__signature__ = signature  # type: ignore[attr-defined]
        _proxy.__annotations__ = {p.name: p.annotation for p in signature.parameters.values()}
        _proxy.__name__ = prefixed_tool_name(alias, orig_name)
        _proxy.__doc__ = tool.description or ""
        return _proxy

    async def _call_downstream(self, alias: str, tool_name: str, arguments: dict[str, Any]) -> Any:
        mount = self.mounts.get(alias)
        if mount is None:
            raise ToolError(
                f"server '{alias}' is not mounted; it was removed or never added. "
                "Call capability_list() to see live servers."
            )
        try:
            result = await mount.session.call_tool(tool_name, arguments)
        except Exception as exc:
            mount.healthy = False
            mount.last_error = str(exc)
            raise ToolError(
                f"downstream server '{alias}' is unavailable: {exc}. "
                "The subprocess may have crashed or hung; use capability_remove() "
                "and capability_add() to remount it."
            ) from exc
        mount.healthy = True
        mount.last_error = None
        return result

    # -- verify -----------------------------------------------------------------

    async def verify(self, alias: str) -> dict[str, Any]:
        """Smoke-test a mounted server: ping, list_tools, and one safe probe call."""
        mount = self.mounts.get(alias)
        if mount is None:
            return {"ok": False, "error": f"no mounted server with alias {alias!r}"}

        evidence: list[str] = []
        passed = True
        probe_name: str | None = None

        try:
            with anyio.fail_after(30):
                await mount.session.send_ping()
            evidence.append("ping ok")
        except BaseException as exc:
            passed = False
            evidence.append(f"ping failed: {exc}")

        live_tools: list[str] = []
        if passed:
            try:
                with anyio.fail_after(30):
                    listed = await mount.session.list_tools()
                live_tools = [t.name for t in listed.tools]
                mount.tools = {t.name: t for t in listed.tools}
                evidence.append(f"list_tools ok ({len(live_tools)} tools)")
            except BaseException as exc:
                passed = False
                evidence.append(f"list_tools failed: {exc}")

        if passed:
            probe = self._pick_probe(mount)
            if probe is None:
                evidence.append("no safe zero-argument probe tool found; skipped live call")
            else:
                probe_name = probe.name
                try:
                    with anyio.fail_after(30):
                        result = await mount.session.call_tool(probe.name, {})
                    if result.is_error:
                        passed = False
                        evidence.append(f"probe '{probe.name}' returned an error")
                    else:
                        snippet = _result_snippet(result)
                        evidence.append(f"probe '{probe.name}' ok: {snippet}")
                except BaseException as exc:
                    passed = False
                    evidence.append(f"probe '{probe.name}' failed: {exc}")

        record = {
            "verified_at": utc_now_iso(),
            "passed": passed,
            "evidence": "; ".join(evidence),
            "tools": live_tools or sorted(mount.tools),
            "source": mount.source,
            "identifier": mount.identifier,
        }
        self.store.record_trust(alias, record)
        self.store.mark_verified(alias, passed)
        self.store.audit("verify", alias=alias, passed=passed, evidence=record["evidence"])
        mount.healthy = passed
        mount.last_error = None if passed else record["evidence"]
        return {
            "ok": True,
            "alias": alias,
            "passed": passed,
            "probe": probe_name,
            "evidence": record["evidence"],
            "verified_at": record["verified_at"],
            "note": "smoke verification proves the server responds; it is not a security audit",
        }

    @staticmethod
    def _pick_probe(mount: Mount) -> MCPTool | None:
        for tool in mount.tools.values():
            schema = tool.input_schema if isinstance(tool.input_schema, dict) else {}
            if PROBE_TOOL_PATTERN.search(tool.name) and not schema.get("required"):
                return tool
        return None

    # -- list / remove ------------------------------------------------------------

    def list_mounts(self) -> dict[str, Any]:
        trust = self.store.load_trust()
        servers: list[dict[str, Any]] = []
        for alias, mount in sorted(self.mounts.items()):
            record = trust.get(alias) or {}
            servers.append(
                {
                    "alias": alias,
                    "source": mount.source,
                    "identifier": mount.identifier,
                    "command": " ".join(mount.argv),
                    "tools": sorted(mount.registered_names),
                    "healthy": mount.healthy,
                    "last_error": mount.last_error,
                    "verified": bool(record.get("passed")),
                    "verified_at": record.get("verified_at"),
                    "mounted_at": mount.mounted_at,
                }
            )
        for alias, entry in sorted(self.store.load_config()["servers"].items()):
            if alias in self.mounts:
                continue
            record = trust.get(alias) or {}
            servers.append(
                {
                    "alias": alias,
                    "source": entry.get("source"),
                    "identifier": entry.get("identifier"),
                    "command": " ".join(entry.get("command") or []),
                    "tools": [prefixed_tool_name(alias, t) for t in entry.get("tools") or []],
                    "healthy": False,
                    "last_error": "not mounted (remount failed or pending)",
                    "verified": bool(record.get("passed") or entry.get("verified")),
                    "verified_at": record.get("verified_at"),
                    "mounted_at": None,
                }
            )
        return {"ok": True, "mounted": len(self.mounts), "servers": servers}

    async def remove(self, alias: str, ctx: Any = None) -> dict[str, Any]:
        """Unmount a server: terminate its subprocess and unregister its tools."""
        async with self._lock:
            mount = self.mounts.pop(alias, None)
            if mount is None:
                return {"ok": False, "error": f"no mounted server with alias {alias!r}"}
            removed_tools = list(mount.registered_names)
            for pname in removed_tools:
                try:
                    self.server.remove_tool(pname)
                except Exception:
                    pass
            mount.close_event.set()
            with anyio.move_on_after(10):
                await mount.closed_event.wait()
            self.store.remove_server(alias)
            self.store.audit("remove", alias=alias, identifier=mount.identifier, tools=removed_tools)
            await self._notify_tools_changed(ctx)
            return {"ok": True, "alias": alias, "removed_tools": removed_tools}

    # -- startup remount ------------------------------------------------------------

    async def remount_verified(self) -> dict[str, Any]:
        """Re-mount servers that were verified in a previous run.

        Only servers whose last verification passed are auto-mounted, and the
        persisted launch command is used directly (no network needed).
        """
        remounted: list[str] = []
        failed: dict[str, str] = {}
        for alias, entry in sorted(self.store.load_config()["servers"].items()):
            if not entry.get("verified"):
                continue
            argv = [str(a) for a in entry.get("command") or []]
            if not argv:
                continue
            try:
                mount = await self._spawn(
                    alias,
                    argv,
                    source=str(entry.get("source", "local")),
                    identifier=str(entry.get("identifier", "")),
                )
            except BrokerError as exc:
                failed[alias] = str(exc)
                self.store.audit("remount_failed", alias=alias, error=str(exc))
                continue
            self.mounts[alias] = mount
            mount.registered_names = self._register_mount_tools(mount)
            remounted.append(alias)
            self.store.audit("remount", alias=alias, tools=sorted(mount.tools))
        if remounted or failed:
            self.store.audit("remount_summary", remounted=remounted, failed=failed)
        return {"remounted": remounted, "failed": failed}

    async def _close_all_mounts(self) -> None:
        for mount in self.mounts.values():
            mount.close_event.set()
        for mount in self.mounts.values():
            with anyio.move_on_after(10):
                await mount.closed_event.wait()
        self.mounts.clear()

    # -- notifications ---------------------------------------------------------------

    async def _notify_tools_changed(self, ctx: Any = None) -> None:
        """Tell the connected client the tool list changed.

        Sends both the classic ``notifications/tools/list_changed`` session
        notification (pre-2026-07-28 clients) and a publish on the
        subscriptions bus (2026-07-28+ ``subscriptions/listen`` streams).
        Failures are logged, never raised: a mounted capability must not be
        rolled back just because the client could not be told about it.
        """
        if ctx is None:
            return
        try:
            await ctx.session.send_tool_list_changed()
        except Exception as exc:
            self.store.audit("notify_warn", via="session", error=str(exc))
        try:
            await ctx.notify_tools_changed()
        except Exception as exc:
            self.store.audit("notify_warn", via="bus", error=str(exc))


def _result_snippet(result: Any, limit: int = 120) -> str:
    parts: list[str] = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    snippet = " | ".join(parts) or "(no text content)"
    return snippet if len(snippet) <= limit else snippet[: limit - 1] + "…"
