"""Console entry point: ``extrovert`` / ``python3 -m extrovert``.

Bare invocation (or ``extrovert serve``) runs the MCP broker over stdio —
that is what agent clients launch. Subcommands handle installation and
diagnostics.
"""

from __future__ import annotations

import argparse
import sys

import anyio

from . import __version__
from .server import serve


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="extrovert",
        description="Capability broker MCP server: discover, install, verify and hot-mount external MCP servers.",
    )
    parser.add_argument("--version", action="version", version=f"extrovert-mcp {__version__}")
    sub = parser.add_subparsers(dest="subcommand")
    sub.add_parser("serve", help="run the MCP broker server over stdio (default)")

    p_install = sub.add_parser("install", help="register extrovert into detected agent clients")
    p_install.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    p_install.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    p_uninstall = sub.add_parser("uninstall", help="remove extrovert from detected agent clients")
    p_uninstall.add_argument("--dry-run", action="store_true", help="print the plan; change nothing")
    p_uninstall.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")

    sub.add_parser("doctor", help="self-check the environment and print a report")
    return parser


def _confirm(question: str) -> bool:
    try:
        return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)

    if args.subcommand in (None, "serve"):
        anyio.run(serve)
        return

    if args.subcommand == "doctor":
        from .doctor import run_doctor

        raise SystemExit(run_doctor())

    from .install import run_install

    uninstall = args.subcommand == "uninstall"
    if args.dry_run:
        raise SystemExit(run_install(dry_run=True, uninstall=uninstall))
    if not args.yes and sys.stdin.isatty():
        action = "remove extrovert from" if uninstall else "register extrovert into"
        if not _confirm(f"This will {action} detected agent client configs. Proceed?"):
            print("aborted; nothing changed.")
            raise SystemExit(0)
    raise SystemExit(run_install(dry_run=False, uninstall=uninstall))


if __name__ == "__main__":
    main()
