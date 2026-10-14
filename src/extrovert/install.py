"""Self-registration of extrovert into agent clients' MCP configuration.

Supported targets:

- Claude Code CLI (``claude mcp add``) — detected via the ``claude`` binary
- Kimi Code — ``~/.kimi-code/mcp.json`` (``mcpServers`` object) or, when the
  existing ``config.toml`` already has an ``[mcp_servers]`` section, a TOML
  table appended to it

When nothing known is detected (or a config file's shape is uncertain), no
file is touched: the caller prints a generic snippet for manual pasting.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .config import ConfigStore

CLAUDE_ADD_TIMEOUT = 30
KIMI_DIR_NAME = ".kimi-code"
MCP_SECTION_PATTERN = re.compile(r"^\s*\[mcp_servers(\.[^\]]+)?\]", re.MULTILINE)
EXTROVERT_TOML_HEADER = "[mcp_servers.extrovert]"

SubprocessRunner = Callable[..., subprocess.CompletedProcess]
WhichFn = Callable[[str], str | None]


@dataclass
class LaunchCommand:
    """How clients should start the extrovert server."""

    argv: list[str]

    @property
    def display(self) -> str:
        return " ".join(self.argv)

    def to_json_entry(self) -> dict[str, Any]:
        entry: dict[str, Any] = {"command": self.argv[0]}
        if len(self.argv) > 1:
            entry["args"] = self.argv[1:]
        return entry


def detect_launch_command(which: WhichFn | None = None) -> LaunchCommand:
    """Prefer the installed ``extrovert`` script; fall back to ``python -m``."""
    which = which or shutil.which
    exe = which("extrovert")
    if exe:
        return LaunchCommand([str(Path(exe).resolve())])
    return LaunchCommand([sys.executable, "-m", "extrovert"])


@dataclass
class ClientTarget:
    kind: str  # "claude-cli" | "kimi-json" | "kimi-toml" | "kimi-manual"
    label: str
    config_path: Path | None = None
    detail: str = ""


def detect_clients(home: Path | None = None, which: WhichFn | None = None) -> list[ClientTarget]:
    """Detect agent clients on this machine. Pure: touches nothing."""
    which = which or shutil.which
    home = home or Path.home()
    targets: list[ClientTarget] = []

    claude = which("claude")
    if claude:
        targets.append(ClientTarget("claude-cli", "Claude Code", detail=claude))

    kimi_dir = home / KIMI_DIR_NAME
    if kimi_dir.is_dir():
        mcp_json = kimi_dir / "mcp.json"
        config_toml = kimi_dir / "config.toml"
        if mcp_json.is_file():
            try:
                data = json.loads(mcp_json.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = None
            if isinstance(data, dict) and isinstance(data.get("mcpServers"), dict):
                targets.append(
                    ClientTarget("kimi-json", "Kimi Code (mcp.json)", config_path=mcp_json)
                )
            else:
                targets.append(
                    ClientTarget(
                        "kimi-manual",
                        "Kimi Code (mcp.json, unrecognized shape)",
                        config_path=mcp_json,
                        detail="mcp.json exists but has no mcpServers object; not guessing",
                    )
                )
        elif config_toml.is_file():
            try:
                text = config_toml.read_text(encoding="utf-8")
            except OSError:
                text = ""
            if MCP_SECTION_PATTERN.search(text):
                targets.append(
                    ClientTarget("kimi-toml", "Kimi Code (config.toml)", config_path=config_toml)
                )
            else:
                targets.append(
                    ClientTarget(
                        "kimi-manual",
                        "Kimi Code (config.toml, no [mcp_servers] section)",
                        config_path=config_toml,
                        detail="config.toml has no [mcp_servers] structure; not guessing",
                    )
                )
        else:
            targets.append(
                ClientTarget(
                    "kimi-manual",
                    "Kimi Code (no MCP config file yet)",
                    config_path=kimi_dir / "mcp.json",
                    detail="no mcp.json/config.toml found; create mcp.json manually",
                )
            )
    return targets


@dataclass
class ActionResult:
    target: ClientTarget
    ok: bool
    changed: bool
    message: str
    planned: list[str] = field(default_factory=list)


def backup_file(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    if backup.exists():
        stamp = datetime.now().strftime("%Y%m%d%H%M%S")
        backup = path.with_name(f"{path.name}.bak.{stamp}")
    shutil.copy2(path, backup)
    return backup


def _default_runner(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
    kwargs.setdefault("capture_output", True)
    kwargs.setdefault("text", True)
    kwargs.setdefault("timeout", CLAUDE_ADD_TIMEOUT)
    return subprocess.run(*args, **kwargs)


# -- per-target install / uninstall -------------------------------------------


def install_claude(
    target: ClientTarget, launch: LaunchCommand, *, dry_run: bool, runner: SubprocessRunner = _default_runner
) -> ActionResult:
    add_cmd = ["claude", "mcp", "add", "-s", "user", "extrovert", "--", *launch.argv]
    planned = ["claude mcp list", " ".join(add_cmd), "claude mcp list"]
    if dry_run:
        return ActionResult(target, True, False, "dry-run: would register via claude CLI", planned)
    try:
        listed = runner(["claude", "mcp", "list"])
        if "extrovert" in (listed.stdout or ""):
            return ActionResult(target, True, False, "already registered with Claude Code", planned)
        added = runner(add_cmd)
        if added.returncode != 0:
            return ActionResult(
                target, False, False, f"claude mcp add failed: {(added.stderr or '').strip()}", planned
            )
        verify = runner(["claude", "mcp", "list"])
        if "extrovert" in (verify.stdout or ""):
            return ActionResult(target, True, True, "registered with Claude Code (user scope)", planned)
        return ActionResult(
            target, False, True, "claude mcp add ran but extrovert not visible in claude mcp list", planned
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(target, False, False, f"claude CLI error: {exc}", planned)


def uninstall_claude(
    target: ClientTarget, *, dry_run: bool, runner: SubprocessRunner = _default_runner
) -> ActionResult:
    planned = ["claude mcp list", "claude mcp remove -s user extrovert"]
    if dry_run:
        return ActionResult(target, True, False, "dry-run: would remove via claude CLI", planned)
    try:
        listed = runner(["claude", "mcp", "list"])
        if "extrovert" not in (listed.stdout or ""):
            return ActionResult(target, True, False, "not registered with Claude Code", planned)
        removed = runner(["claude", "mcp", "remove", "-s", "user", "extrovert"])
        if removed.returncode != 0:
            return ActionResult(
                target, False, False, f"claude mcp remove failed: {(removed.stderr or '').strip()}", planned
            )
        return ActionResult(target, True, True, "removed from Claude Code", planned)
    except (OSError, subprocess.SubprocessError) as exc:
        return ActionResult(target, False, False, f"claude CLI error: {exc}", planned)


def install_kimi_json(target: ClientTarget, launch: LaunchCommand, *, dry_run: bool) -> ActionResult:
    assert target.config_path is not None
    path = target.config_path
    planned = [f"backup {path}", f"add mcpServers.extrovert to {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ActionResult(target, False, False, f"cannot read {path}: {exc}", planned)
    servers = data.setdefault("mcpServers", {})
    if "extrovert" in servers:
        return ActionResult(target, True, False, f"already present in {path}", planned)
    if dry_run:
        return ActionResult(target, True, False, f"dry-run: would add mcpServers.extrovert to {path}", planned)
    backup_file(path)
    servers["extrovert"] = launch.to_json_entry()
    ConfigStore._save(path, data)
    return ActionResult(target, True, True, f"added mcpServers.extrovert to {path} (backup: {path}.bak)", planned)


def uninstall_kimi_json(target: ClientTarget, *, dry_run: bool) -> ActionResult:
    assert target.config_path is not None
    path = target.config_path
    planned = [f"backup {path}", f"remove mcpServers.extrovert from {path}"]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return ActionResult(target, False, False, f"cannot read {path}: {exc}", planned)
    servers = data.get("mcpServers")
    if not isinstance(servers, dict) or "extrovert" not in servers:
        return ActionResult(target, True, False, f"not present in {path}", planned)
    if dry_run:
        return ActionResult(target, True, False, f"dry-run: would remove mcpServers.extrovert from {path}", planned)
    backup_file(path)
    del servers["extrovert"]
    ConfigStore._save(path, data)
    return ActionResult(target, True, True, f"removed mcpServers.extrovert from {path}", planned)


def _toml_block(launch: LaunchCommand) -> str:
    args = ""
    if len(launch.argv) > 1:
        rendered = ", ".join(json.dumps(a) for a in launch.argv[1:])
        args = f"args = [{rendered}]\n"
    return f"\n{EXTROVERT_TOML_HEADER}\ncommand = {json.dumps(launch.argv[0])}\n{args}"


def install_kimi_toml(target: ClientTarget, launch: LaunchCommand, *, dry_run: bool) -> ActionResult:
    assert target.config_path is not None
    path = target.config_path
    planned = [f"backup {path}", f"append {EXTROVERT_TOML_HEADER} to {path}"]
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        return ActionResult(target, False, False, f"cannot read {path}: {exc}", planned)
    if EXTROVERT_TOML_HEADER in text:
        return ActionResult(target, True, False, f"already present in {path}", planned)
    if dry_run:
        return ActionResult(target, True, False, f"dry-run: would append {EXTROVERT_TOML_HEADER} to {path}", planned)
    backup_file(path)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(_toml_block(launch))
    return ActionResult(target, True, True, f"appended {EXTROVERT_TOML_HEADER} to {path} (backup: {path}.bak)", planned)


def uninstall_kimi_toml(target: ClientTarget, *, dry_run: bool) -> ActionResult:
    assert target.config_path is not None
    path = target.config_path
    planned = [f"backup {path}", f"remove {EXTROVERT_TOML_HEADER} block from {path}"]
    try:
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        return ActionResult(target, False, False, f"cannot read {path}: {exc}", planned)
    start = next((i for i, line in enumerate(lines) if line.strip() == EXTROVERT_TOML_HEADER), None)
    if start is None:
        return ActionResult(target, True, False, f"not present in {path}", planned)
    if dry_run:
        return ActionResult(target, True, False, f"dry-run: would remove {EXTROVERT_TOML_HEADER} block from {path}", planned)
    end = len(lines)
    for i in range(start + 1, len(lines)):
        stripped = lines[i].strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            end = i
            break
    backup_file(path)
    del lines[start:end]
    # drop the blank separator line left behind at the join point
    while start > 0 and start < len(lines) and not lines[start].strip() and not lines[start - 1].strip():
        del lines[start]
    path.write_text("".join(lines), encoding="utf-8")
    return ActionResult(target, True, True, f"removed {EXTROVERT_TOML_HEADER} block from {path}", planned)


# -- top-level orchestration -----------------------------------------------------


def generic_snippet(launch: LaunchCommand) -> str:
    return json.dumps({"mcpServers": {"extrovert": launch.to_json_entry()}}, indent=2)


def manual_snippet(target: ClientTarget, launch: LaunchCommand) -> str:
    if target.kind == "kimi-manual" and target.config_path and target.config_path.suffix == ".toml":
        return _toml_block(launch).strip("\n")
    return generic_snippet(launch)


def run_install(
    *,
    dry_run: bool = False,
    uninstall: bool = False,
    home: Path | None = None,
    which: WhichFn | None = None,
    runner: SubprocessRunner = _default_runner,
    out: Callable[[str], None] = print,
) -> int:
    """Register (or unregister) extrovert with every detected client."""
    launch = detect_launch_command(which)
    targets = detect_clients(home=home, which=which)
    verb = "uninstall" if uninstall else "install"

    out(f"extrovert launch command: {launch.display}")
    if not targets:
        out("no known agent client detected.")
        out("add this to your client's MCP configuration manually:")
        out(generic_snippet(launch))
        return 0

    exit_code = 0
    for target in targets:
        out(f"\n[{target.label}] {target.detail}")
        if target.kind == "claude-cli":
            result = (
                uninstall_claude(target, dry_run=dry_run, runner=runner)
                if uninstall
                else install_claude(target, launch, dry_run=dry_run, runner=runner)
            )
        elif target.kind == "kimi-json":
            result = (
                uninstall_kimi_json(target, dry_run=dry_run)
                if uninstall
                else install_kimi_json(target, launch, dry_run=dry_run)
            )
        elif target.kind == "kimi-toml":
            result = (
                uninstall_kimi_toml(target, dry_run=dry_run)
                if uninstall
                else install_kimi_toml(target, launch, dry_run=dry_run)
            )
        else:  # kimi-manual — never guess an unknown format
            if uninstall:
                out("  nothing to remove automatically; edit the config by hand if you added extrovert.")
                continue
            out(f"  {target.detail}.")
            out("  add this snippet yourself:")
            for line in manual_snippet(target, launch).splitlines():
                out(f"    {line}")
            continue
        for step in result.planned:
            out(f"  plan: {step}")
        out(f"  {'ok' if result.ok else 'FAILED'}: {result.message}")
        if not result.ok:
            exit_code = 1
    return exit_code
