"""Console entry point: ``extrovert`` / ``python3 -m extrovert``."""

from __future__ import annotations

import anyio

from .server import serve


def main() -> None:
    anyio.run(serve)


if __name__ == "__main__":
    main()
