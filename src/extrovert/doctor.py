"""``extrovert doctor``: environment self-check with a plain-text report."""

from __future__ import annotations

import importlib.metadata
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import __version__
from .hint import HINT_TEXT
from .registry import registry_base_url

REGISTRY_PROBE_TIMEOUT = 3.0
HINT_PROBE_TIMEOUT = 5.0


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    critical: bool = True


def probe_registry(timeout: float = REGISTRY_PROBE_TIMEOUT) -> tuple[bool, str]:
    """Lightweight GET against the registry; returns (reachable, detail)."""
    from .registry import _http_json  # late import keeps doctor import cheap

    url = f"{registry_base_url()}/v0/servers?limit=1"
    try:
        _http_json(url, timeout=timeout)
    except Exception as exc:
        return False, f"{registry_base_url()} unreachable: {exc}"
    return True, f"{registry_base_url()} responded"


def probe_hint() -> tuple[bool, str]:
    """Run the resolved launch command with the hint subcommand."""
    from .install import detect_launch_command

    launch = detect_launch_command()
    cmd = [*launch.argv, "hint"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=HINT_PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"'{launch.display} hint' failed to run: {exc}"
    if proc.returncode != 0 or HINT_TEXT not in proc.stdout:
        return False, f"'{launch.display} hint' exited {proc.returncode}: {(proc.stderr or '').strip()[:120]}"
    return True, f"'{launch.display} hint' works"


def check_state_dir(home: Path) -> tuple[bool, str]:
    try:
        home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=home, prefix=".doctor-", delete=True):
            pass
    except OSError as exc:
        return False, f"{home} not writable: {exc}"
    return True, f"{home} writable"


def run_checks(
    *,
    which: Callable[[str], str | None] = shutil.which,
    registry_probe: Callable[[], tuple[bool, str]] | None = None,
    hint_probe: Callable[[], tuple[bool, str]] | None = None,
    state_home: Path | None = None,
) -> list[Check]:
    from .config import extrovert_home

    probe = registry_probe or probe_registry
    hint = hint_probe or probe_hint

    checks: list[Check] = []

    py = sys.version_info
    checks.append(
        Check(
            "python",
            py >= (3, 10),
            f"{py.major}.{py.minor}.{py.micro} ({sys.executable})"
            + ("" if py >= (3, 10) else " — extrovert requires Python >= 3.10"),
        )
    )

    try:
        mcp_version = importlib.metadata.version("mcp")
        major = int(mcp_version.split(".")[0])
        checks.append(Check("mcp-sdk", major == 2, f"mcp {mcp_version}"))
    except (importlib.metadata.PackageNotFoundError, ValueError, IndexError) as exc:
        checks.append(Check("mcp-sdk", False, f"mcp SDK not importable: {exc}"))

    checks.append(Check("extrovert", True, f"extrovert-mcp {__version__}", critical=False))

    npx = which("npx")
    checks.append(
        Check(
            "npx",
            npx is not None,
            npx or "not found — registry npm servers need npx (install Node.js)",
            critical=False,
        )
    )
    uvx = which("uvx")
    checks.append(
        Check(
            "uvx",
            uvx is not None,
            uvx or "not found — pypi servers fall back to 'python3 -m <pkg>' (install uv for auto-install)",
            critical=False,
        )
    )

    reachable, detail = probe()
    checks.append(Check("registry", reachable, detail, critical=False))

    hint_ok, hint_detail = hint()
    checks.append(Check("hint-command", hint_ok, hint_detail, critical=False))

    home = state_home or extrovert_home()
    writable, detail = check_state_dir(home)
    checks.append(Check("state-dir", writable, detail))

    return checks


def render_report(checks: list[Check]) -> str:
    lines = ["extrovert doctor", ""]
    for check in checks:
        if check.ok:
            mark = "ok " if check.critical else "ok (optional)"
        else:
            mark = "FAIL" if check.critical else "warn"
        lines.append(f"[{mark:>13}] {check.name}: {check.detail}")
    failed = [c for c in checks if not c.ok and c.critical]
    warned = [c for c in checks if not c.ok and not c.critical]
    lines.append("")
    if failed:
        lines.append(f"{len(failed)} critical problem(s) found; fix them before using extrovert.")
    elif warned:
        lines.append("all critical checks passed; some optional features are degraded.")
    else:
        lines.append("all checks passed.")
    return "\n".join(lines)


def run_doctor(
    *,
    which: Callable[[str], str | None] = shutil.which,
    registry_probe: Callable[[], tuple[bool, str]] | None = None,
    hint_probe: Callable[[], tuple[bool, str]] | None = None,
    state_home: Path | None = None,
    out: Callable[[str], None] = print,
) -> int:
    checks = run_checks(
        which=which, registry_probe=registry_probe, hint_probe=hint_probe, state_home=state_home
    )
    out(render_report(checks))
    return 1 if any(not c.ok and c.critical for c in checks) else 0
