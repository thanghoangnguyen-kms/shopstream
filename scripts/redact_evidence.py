"""Redact a capture on stdin before it is written to docs/evidence.

Every capture for docs/evidence passes through this filter first:

    some-command 2>&1 | uv run python scripts/redact_evidence.py > capture.txt

The pipeline is three ordered layers, so a secret that contains a path fragment is masked
whole before any path is rewritten:

1. Shapes: credential-named values (JSON, YAML, properties, KEY=value, dict repr, `=>`),
   DSN passwords, STS XML secret elements, auth headers and JWTs.
2. Literals: every value in infra/.env of length 8 or more, longest first. It runs only
   when that file exists.
3. Paths: the repo root becomes `<repo>` and the home directory becomes `<home>`.

The mask is the literal `REDACTED`, the same convention as docs/evidence/w1-prove.md. The
output is idempotent. No value is ever printed on an error.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path

from dotenv_lite import DEFAULT_ENV_FILE, REPO_ROOT, DotenvError, read_dotenv

REDACTED = "REDACTED"
MIN_LITERAL_LENGTH = 8
REPO_TOKEN = "<repo>"
HOME_TOKEN = "<home>"

# Access key ids (`s3.access-key-id`, `AWS_ACCESS_KEY_ID`) end in "id", so they are listed too.
SECRET_SUFFIXES = (
    "key",
    "secret",
    "password",
    "passwd",
    "token",
    "credential",
    "credentials",
    "accesskeyid",
)
# Names that end in "key" but are not secrets, compared with separators removed.
NON_SECRET_KEY_SUFFIXES = ("primarykey", "partitionkey", "messagekey", "sortkey", "foreignkey")

KEY_VALUE = re.compile(
    r"""
    (?P<pre>
        (?P<name>[A-Za-z0-9_.\-]*[A-Za-z0-9])
        ["']?[ \t]*
        (?:=>|=|:(?=[ \t"']))
        [ \t]*
    )
    (?:
        (?P<q>["'])(?P<qv>(?:(?!(?P=q))[^\r\n])+)(?P=q)
        |
        (?P<v>[^\s"',}\]]+)
    )
    """,
    re.VERBOSE,
)
DSN = re.compile(r"(?P<pre>[a-zA-Z][a-zA-Z0-9+.-]*://[^\s:/@]+:)(?P<pw>[^\s@/]+)(?=@)")
XML = re.compile(
    r"(?P<open><(?P<tag>[A-Za-z]*(?:Secret|Token|Password|Credential|AccessKey)[A-Za-z]*)>)"
    r"(?P<v>[^<]+)(?P<close></(?P=tag)>)"
)
HEADER = re.compile(
    r"(?im)^(?P<k>[ \t]*(?:authorization|x-amz-security-token|x-amz-signature|x-api-key)"
    r"[ \t]*:[ \t]*)(?P<v>.+)$"
)
JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*")


def is_secret_name(name: str) -> bool:
    """True when a key name carries a credential, so its value must be masked."""
    bare = re.sub(r"[_.\-]", "", name.lower())
    if bare == "key" or bare.endswith(NON_SECRET_KEY_SUFFIXES):
        return False
    return bare.endswith(SECRET_SUFFIXES)


def mask_key_value(match: re.Match[str]) -> str:
    if not is_secret_name(match.group("name")):
        return match.group(0)
    quote = match.group("q")
    if quote is not None:
        return f"{match.group('pre')}{quote}{REDACTED}{quote}"
    return f"{match.group('pre')}{REDACTED}"


def mask_xml(match: re.Match[str]) -> str:
    if not match.group("v").strip():
        return match.group(0)
    return f"{match.group('open')}{REDACTED}{match.group('close')}"


def redact_shapes(text: str) -> str:
    """Layer 1: mask values by shape, keeping quotes, separators and structure."""
    text = KEY_VALUE.sub(mask_key_value, text)
    text = DSN.sub(lambda m: f"{m.group('pre')}{REDACTED}", text)
    text = XML.sub(mask_xml, text)
    text = HEADER.sub(lambda m: f"{m.group('k')}{REDACTED}", text)
    return JWT.sub(REDACTED, text)


def redact_literals(text: str, values: Iterable[str]) -> str:
    """Layer 2: mask each literal of at least MIN_LITERAL_LENGTH, longest first."""
    usable = {value for value in values if len(value) >= MIN_LITERAL_LENGTH}
    for value in sorted(usable, key=lambda item: (-len(item), item)):
        text = text.replace(value, REDACTED)
    return text


def path_forms(path: Path) -> set[str]:
    forms = {str(path), str(path.resolve())}
    return {form.rstrip("/") for form in forms if len(form.rstrip("/")) > 1}


def redact_paths(text: str, repo_root: Path, home: Path | Sequence[Path]) -> str:
    """Layer 3: rewrite the repo root to `<repo>` first, then the home directory."""
    homes = [home] if isinstance(home, Path) else list(home)
    for token, roots in ((REPO_TOKEN, [repo_root]), (HOME_TOKEN, homes)):
        forms = {form for root in roots for form in path_forms(root)}
        for form in sorted(forms, key=lambda item: (-len(item), item)):
            text = text.replace(form, token)
    return text


def redact(
    text: str, env_values: Iterable[str], repo_root: Path, home: Path | Sequence[Path]
) -> str:
    """Apply shapes, then infra/.env literals, then paths."""
    return redact_paths(redact_literals(redact_shapes(text), env_values), repo_root, home)


def home_directories() -> list[Path]:
    homes = [Path.home()]
    from_env = os.environ.get("HOME")
    if from_env and Path(from_env) not in homes:
        homes.append(Path(from_env))
    return homes


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=DEFAULT_ENV_FILE,
        help="dotenv file whose values are masked as literals (default: infra/.env)",
    )
    args = parser.parse_args(argv)
    try:
        env = read_dotenv(args.env_file)
    except DotenvError as error:
        print(f"redact_evidence: {error}", file=sys.stderr)
        return 2
    values = list(env.values()) if env is not None else []
    sys.stdout.write(redact(sys.stdin.read(), values, REPO_ROOT, home_directories()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
