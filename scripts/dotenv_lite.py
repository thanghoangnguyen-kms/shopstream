"""A tiny KEY=VALUE reader for infra/.env, shared by its three callers.

The callers are `redact_evidence.py` (masks every value it finds), `canary_guard.py` (reads
CANARY_TOKEN) and `stack.py` (tops up the file). infra/.env is written by a machine, so
there is no quote or escape processing and no variable expansion. Error messages name a
line number or a key, never a value, because the values are secrets.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ENV_FILE = REPO_ROOT / "infra" / ".env"

KEY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]*$")


class DotenvError(ValueError):
    """The env file is malformed. The message never contains a value."""


def parse_dotenv(text: str) -> dict[str, str]:
    """Parse KEY=VALUE lines into an insertion-ordered dict.

    Blank lines and lines starting with `#` are ignored. Each other line splits on the
    first `=`; the key must be UPPER_SNAKE and the value is the rest, stripped.
    """
    values: dict[str, str] = {}
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise DotenvError(f"line {number}: expected KEY=VALUE")
        key = key.strip()
        if not KEY_PATTERN.match(key):
            raise DotenvError(f"line {number}: invalid key")
        if key in values:
            raise DotenvError(f"duplicate key {key}")
        values[key] = value.strip()
    return values


def read_dotenv(path: Path) -> dict[str, str] | None:
    """Return the parsed file, or None when it does not exist."""
    if not path.is_file():
        return None
    return parse_dotenv(path.read_text(encoding="utf-8"))
