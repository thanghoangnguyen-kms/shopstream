"""The `canary-guard` prek hook: fail when a file holds the erasure canary token.

The token is CANARY_TOKEN in infra/.env. prek passes the staged file paths; every one is read
as bytes and checked for the token. Only offending paths are printed, never the token or any
file content. Without infra/.env, or without a CANARY_TOKEN in it, the hook is a no-op that
passes, so CI's lint job (which has no infra/.env) is unaffected.

Exit codes: 0 clean or no canary configured, 1 a file holds the token, 2 unusable token.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable
from pathlib import Path

from dotenv_lite import DEFAULT_ENV_FILE, read_dotenv

CANARY_KEY = "CANARY_TOKEN"
MIN_TOKEN_LENGTH = 16


def load_canary(env_file: Path) -> str | None:
    """The canary token, or None when the file or the key is absent or the value is empty."""
    values = read_dotenv(env_file)
    if values is None:
        return None
    return values.get(CANARY_KEY) or None


def files_containing(token: str, paths: Iterable[Path]) -> list[Path]:
    """The paths whose bytes contain the token, in input order; unreadable paths are skipped."""
    needle = token.encode()
    hits: list[Path] = []
    for path in paths:
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if needle in data:
            hits.append(path)
    return hits


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files to check (prek passes these)")
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="dotenv file holding CANARY_TOKEN (default: infra/.env)",
    )
    args = parser.parse_args(argv)
    token = load_canary(args.env_file)
    if token is None:
        return 0
    if len(token) < MIN_TOKEN_LENGTH:
        print(
            f"canary-guard: {CANARY_KEY} in the env file is shorter than {MIN_TOKEN_LENGTH} characters"
        )
        return 2
    hits = files_containing(token, args.paths)
    for path in hits:
        print(f"canary-guard: {CANARY_KEY} from infra/.env found in {path}")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())
