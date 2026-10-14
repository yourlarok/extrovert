"""Persistence, environment scrubbing and audit logging for extrovert.

All state lives under ``~/.extrovert`` (override with the ``EXTROVERT_HOME``
environment variable):

- ``config.json``  — mounted servers and user settings
- ``trust.json``   — verification records written by ``capability_verify``
- ``broker.log``   — JSON-lines audit log of install/remove/verify events
- ``logs/``        — per-server stderr capture of mounted subprocesses
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Environment variables forwarded to mounted subprocesses by default.
# Everything else in the parent environment (API keys, tokens, ...) is
# withheld. Users can opt additional variables in via ``env_passthrough``
# in config.json.
DEFAULT_ENV_WHITELIST: tuple[str, ...] = (
    # POSIX
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    # Windows (absent and harmless elsewhere)
    "SYSTEMROOT",
    "COMSPEC",
    "PATHEXT",
    "WINDIR",
    "PROGRAMDATA",
    "TEMP",
    "TMP",
)

CONFIG_VERSION = 1


def extrovert_home() -> Path:
    override = os.environ.get("EXTROVERT_HOME")
    if override:
        return Path(override).expanduser()
    return Path.home() / ".extrovert"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class ConfigStore:
    """Reads and writes all extrovert state under one home directory."""

    def __init__(self, home: Path | None = None) -> None:
        self.home = home or extrovert_home()
        self.config_path = self.home / "config.json"
        self.trust_path = self.home / "trust.json"
        self.log_path = self.home / "broker.log"
        self.logs_dir = self.home / "logs"

    # -- generic JSON helpers ------------------------------------------------

    @staticmethod
    def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return default
        except (json.JSONDecodeError, OSError):
            return default
        return data if isinstance(data, dict) else default

    @staticmethod
    def _save(path: Path, data: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, indent=2, ensure_ascii=False, sort_keys=True)
                fh.write("\n")
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise

    # -- config.json ----------------------------------------------------------

    def load_config(self) -> dict[str, Any]:
        cfg = self._load(self.config_path, {})
        cfg.setdefault("version", CONFIG_VERSION)
        cfg.setdefault("env_passthrough", [])
        cfg.setdefault("servers", {})
        if not isinstance(cfg["servers"], dict):
            cfg["servers"] = {}
        if not isinstance(cfg["env_passthrough"], list):
            cfg["env_passthrough"] = []
        return cfg

    def save_config(self, cfg: dict[str, Any]) -> None:
        self._save(self.config_path, cfg)

    def record_server(self, entry: dict[str, Any]) -> None:
        cfg = self.load_config()
        cfg["servers"][entry["alias"]] = entry
        self.save_config(cfg)

    def remove_server(self, alias: str) -> None:
        cfg = self.load_config()
        if alias in cfg["servers"]:
            del cfg["servers"][alias]
            self.save_config(cfg)

    def mark_verified(self, alias: str, verified: bool) -> None:
        cfg = self.load_config()
        if alias in cfg["servers"]:
            cfg["servers"][alias]["verified"] = verified
            self.save_config(cfg)

    def env_passthrough(self) -> list[str]:
        return [str(v) for v in self.load_config()["env_passthrough"]]

    # -- trust.json -----------------------------------------------------------

    def load_trust(self) -> dict[str, Any]:
        return self._load(self.trust_path, {})

    def record_trust(self, alias: str, record: dict[str, Any]) -> None:
        trust = self.load_trust()
        trust[alias] = record
        self._save(self.trust_path, trust)

    # -- audit log ------------------------------------------------------------

    def audit(self, event: str, **fields: Any) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        line = json.dumps(
            {"ts": utc_now_iso(), "event": event, **fields},
            ensure_ascii=False,
            default=str,
        )
        with self.log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    # -- environment scrubbing -------------------------------------------------

    def scrub_env(self, extra_names: tuple[str, ...] = ()) -> dict[str, str]:
        """Build a minimal environment for a mounted subprocess.

        Only variables from ``DEFAULT_ENV_WHITELIST`` plus anything the user
        listed in ``env_passthrough`` are inherited from the parent process.
        """
        allowed = set(DEFAULT_ENV_WHITELIST)
        allowed.update(extra_names)
        allowed.update(self.env_passthrough())
        return {k: v for k, v in os.environ.items() if k in allowed}
