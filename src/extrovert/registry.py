"""Client for the official MCP Registry (registry.modelcontextprotocol.io).

Real API shape (v0, probed 2026-10):

- ``GET /v0/servers?search={q}&version=latest&limit={n}``
  returns ``{"servers": [{"server": {...}, "_meta": {...}}], "metadata": {...}}``
- ``GET /v0/servers/{url-encoded-name}/versions/latest``
  returns a single ``{"server": {...}, "_meta": {...}}`` envelope

Each ``server`` object carries ``name`` (e.g. ``com.example/foo``),
``description``, ``version``, optional ``packages`` and ``remotes``.
Each package entry has ``registryType`` (``npm`` / ``pypi`` / ...),
``identifier`` (the installable package name), optional ``transport``
(``{"type": "stdio"}``) and optional ``environmentVariables``.

HTTP is done with the standard library inside a worker thread so extrovert
adds no dependency beyond the ``mcp`` SDK itself.
"""

from __future__ import annotations

import json
import os
import shutil
import ssl
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

import anyio

from . import __version__

DEFAULT_REGISTRY_BASE_URL = "https://registry.modelcontextprotocol.io"
HTTP_TIMEOUT_SECONDS = 30.0


def _ssl_context() -> ssl.SSLContext:
    """Default TLS context; prefer certifi's CA bundle when available.

    Framework Pythons on macOS ship without the system keychain wired into
    ssl, so urllib alone fails certificate verification there.
    """
    try:
        import certifi

        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


def registry_base_url() -> str:
    return os.environ.get("EXTROVERT_REGISTRY_URL", DEFAULT_REGISTRY_BASE_URL).rstrip("/")


class RegistryError(Exception):
    """Raised when the registry is unreachable, or an entry cannot be used."""


@dataclass
class PackageInfo:
    registry_type: str
    identifier: str
    version: str | None = None
    transport: str | None = None
    required_env: list[str] = field(default_factory=list)


@dataclass
class ServerEntry:
    name: str
    description: str
    version: str
    title: str | None = None
    packages: list[PackageInfo] = field(default_factory=list)
    remotes: list[dict[str, Any]] = field(default_factory=list)

    @property
    def installable(self) -> bool:
        return any(p.registry_type in ("npm", "pypi") for p in self.packages)

    @property
    def remote_only(self) -> bool:
        return bool(self.remotes) and not self.installable

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "packages": [
                {"registryType": p.registry_type, "identifier": p.identifier, "version": p.version}
                for p in self.packages
            ],
            "installable": self.installable,
            "remote_only": self.remote_only,
        }


@dataclass
class LaunchPlan:
    """How to launch a registry server as a local stdio subprocess."""

    argv: list[str]
    registry_type: str
    package: str
    missing_required_env: list[str] = field(default_factory=list)


def _parse_package(raw: dict[str, Any]) -> PackageInfo:
    transport = raw.get("transport")
    transport_type = transport.get("type") if isinstance(transport, dict) else None
    required_env = [
        str(v["name"])
        for v in raw.get("environmentVariables") or []
        if isinstance(v, dict) and v.get("isRequired") and v.get("name")
    ]
    return PackageInfo(
        registry_type=str(raw.get("registryType", "")),
        identifier=str(raw.get("identifier", "")),
        version=str(raw["version"]) if raw.get("version") else None,
        transport=transport_type,
        required_env=required_env,
    )


def parse_server(raw: dict[str, Any]) -> ServerEntry:
    """Parse one ``server`` object from a registry payload."""
    return ServerEntry(
        name=str(raw.get("name", "")),
        title=raw.get("title") or None,
        description=str(raw.get("description", "")),
        version=str(raw.get("version", "")),
        packages=[_parse_package(p) for p in raw.get("packages") or [] if isinstance(p, dict)],
        remotes=[r for r in raw.get("remotes") or [] if isinstance(r, dict)],
    )


def parse_server_list(payload: dict[str, Any]) -> list[ServerEntry]:
    """Parse the ``GET /v0/servers`` response into entries (pure, testable)."""
    entries = []
    for item in payload.get("servers") or []:
        if not isinstance(item, dict):
            continue
        raw = item.get("server", item)
        if isinstance(raw, dict) and raw.get("name"):
            entries.append(parse_server(raw))
    return entries


def _http_json(url: str) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"extrovert-mcp/{__version__}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS, context=_ssl_context()) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RegistryError(f"not found in the MCP Registry: {url}") from exc
        raise RegistryError(f"registry returned HTTP {exc.code} for {url}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RegistryError(
            f"MCP Registry is unreachable ({exc}). Check your network connection; "
            "capability_search needs access to registry.modelcontextprotocol.io."
        ) from exc
    try:
        data = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"registry returned invalid JSON for {url}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"registry returned an unexpected payload for {url}")
    return data


async def search_servers(query: str, limit: int = 5) -> list[ServerEntry]:
    """Search the official registry. Raises RegistryError on failure."""
    params = urllib.parse.urlencode({"search": query, "version": "latest", "limit": max(1, min(limit, 50))})
    payload = await anyio.to_thread.run_sync(_http_json, f"{registry_base_url()}/v0/servers?{params}")
    return parse_server_list(payload)


async def get_server(name: str) -> ServerEntry:
    """Fetch one server by exact registry name. Raises RegistryError if absent."""
    quoted = urllib.parse.quote(name, safe="")
    payload = await anyio.to_thread.run_sync(
        _http_json, f"{registry_base_url()}/v0/servers/{quoted}/versions/latest"
    )
    raw = payload.get("server", payload)
    if not isinstance(raw, dict) or not raw.get("name"):
        raise RegistryError(f"registry returned no server object for {name!r}")
    return parse_server(raw)


def resolve_launch_command(entry: ServerEntry) -> LaunchPlan:
    """Turn a registry entry into a stdio launch command.

    npm packages are launched as ``npx -y <identifier>[@<version>]``.
    pypi packages use ``uvx <identifier>`` when uvx is on PATH, otherwise
    ``python3 -m <identifier>`` (which requires the package to be installed).
    """
    chosen: PackageInfo | None = None
    for pkg in entry.packages:
        if pkg.registry_type not in ("npm", "pypi") or not pkg.identifier:
            continue
        if pkg.transport not in (None, "stdio"):
            continue
        chosen = pkg
        break
    if chosen is None:
        if entry.remotes:
            raise RegistryError(
                f"{entry.name!r} only offers a remote (streamable-http) endpoint; "
                "remote transport is not supported by extrovert yet"
            )
        raise RegistryError(f"{entry.name!r} has no installable npm/pypi stdio package")

    if chosen.registry_type == "npm":
        spec = f"{chosen.identifier}@{chosen.version}" if chosen.version else chosen.identifier
        argv = ["npx", "-y", spec]
    else:
        uvx = shutil.which("uvx")
        argv = [uvx, chosen.identifier] if uvx else ["python3", "-m", chosen.identifier]

    missing_env = [name for name in chosen.required_env if not os.environ.get(name)]
    return LaunchPlan(
        argv=argv,
        registry_type=chosen.registry_type,
        package=chosen.identifier,
        missing_required_env=missing_env,
    )
