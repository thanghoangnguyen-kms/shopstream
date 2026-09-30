"""The `canary-guard` prek hook: fail when a file holds the erasure canary token.

The token is CANARY_TOKEN in infra/.env. This is a stub written to prove the tests fail
first; the implementation follows in the next commit.
"""

from __future__ import annotations

import sys
from collections.abc import Iterable
from pathlib import Path

CANARY_KEY = "CANARY_TOKEN"
MIN_TOKEN_LENGTH = 16


def load_canary(env_file: Path) -> str | None:
    return None


def files_containing(token: str, paths: Iterable[Path]) -> list[Path]:
    return []


def main(argv: list[str] | None = None) -> int:
    return 0


if __name__ == "__main__":
    sys.exit(main())
