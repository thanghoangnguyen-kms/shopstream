"""Redact a capture on stdin before it is written to docs/evidence.

Every capture for docs/evidence passes through this filter first:

    some-command 2>&1 | uv run python scripts/redact_evidence.py > capture.txt

The pipeline is three ordered layers, so a secret that contains a path fragment is masked
whole before any path is rewritten:

1. Shapes: credential-named values (JSON, YAML, properties, KEY=value, dict repr, `=>`),
   DSN passwords, STS XML secret elements, auth headers and JWTs. A non-secret pair never hides
   a credential that follows it (`user=bob&password=X` masks X), and a secret value is masked
   whole, however many words, separators or escaped quotes it holds.
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
    "signature",
    "authorization",
    "cookie",
)
# Names that end in "key" but are not secrets, compared with separators removed.
NON_SECRET_KEY_SUFFIXES = (
    "primarykey",
    "partitionkey",
    "messagekey",
    "sortkey",
    "foreignkey",
    "uniquekey",
)

# A name and its separator, nothing more. The lookbehind makes a match start only at the
# beginning of a run of name characters, which keeps the search linear on long lines.
NAME_SEP = re.compile(
    r"""
    (?<![A-Za-z0-9_.\-])
    (?P<name>[A-Za-z0-9_.\-]*[A-Za-z0-9])
    ["']?[ \t]*
    (?P<sep>=>|=|:(?=[ \t"']))
    [ \t]*
    """,
    re.VERBOSE,
)
# What may precede a line-level assignment: indentation, `export`, a YAML list dash, a quote.
LINE_LEAD = re.compile(r"""[ \t]*(?:export[ \t]+)?(?:-[ \t]+)?["']?""")
DOUBLE_QUOTED = re.compile(r'"((?:[^"\\\r\n]|\\.)*)"')
SINGLE_QUOTED = re.compile(r"'((?:[^'\\\r\n]|\\.)*)'")
TO_LINE_END = re.compile(r"[^\r\n]*")
TO_QUOTE_OR_LINE_END = re.compile(r"""[^\r\n"']*""")
# `&` or `;` that opens another `name=` pair: where a line-level `KEY=value` value stops.
PAIR_JOIN = re.compile(r"[&;](?=[A-Za-z0-9_.\-]+=)")
TO_TERMINATOR = re.compile(r"""[^\s&;,"'}\])]*""")
EMPTY_TERMINATORS = "&;,}])"
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


def match_end(pattern: re.Pattern[str], text: str, start: int) -> int:
    """Where `pattern` ends when matched at `start`; `start` itself when it does not match."""
    found = pattern.match(text, start)
    return found.end() if found is not None else start


def value_span(text: str, start: int, sep: str, line_level: bool) -> tuple[int, int] | None:
    """The span of the secret value that begins at `start`, or None when the value is empty.

    The span never crosses a line end and never includes trailing spaces, tabs or a carriage
    return. Quotes around the value are outside the span.
    """
    if start >= len(text) or text[start] in "\r\n":
        return None
    first = text[start]
    if first in "\"'":
        quoted = (DOUBLE_QUOTED if first == '"' else SINGLE_QUOTED).match(text, start)
        if quoted is not None:
            inner = quoted.span(1)
            return inner if inner[0] != inner[1] else None
        start += 1
        end = match_end(TO_LINE_END, text, start)
    elif sep != ":" and first in EMPTY_TERMINATORS:
        return None
    elif line_level:
        end = match_end(TO_LINE_END, text, start)
        join = PAIR_JOIN.search(text, start, end) if sep != ":" else None
        if join is not None:
            return (start, join.start()) if join.start() > start else None
    elif sep == ":":
        end = match_end(TO_QUOTE_OR_LINE_END, text, start)
    else:
        end = match_end(TO_TERMINATOR, text, start)
    end = start + len(text[start:end].rstrip(" \t"))
    return (start, end) if end > start else None


def mask_key_values(text: str) -> str:
    """Mask the value of every credential-named pair, scanning left to right.

    A non-secret pair consumes only its name and separator, so a pair nested in its value
    (`user=bob&password=X`, `msg="failed password=X"`) is still found.
    """
    pieces: list[str] = []
    copied = 0
    scan = 0
    line_start = 0
    line_checked = 0
    lead_line = -1
    lead_end = 0
    while (match := NAME_SEP.search(text, scan)) is not None:
        scan = match.end()
        if not is_secret_name(match.group("name")):
            continue
        newline = text.rfind("\n", line_checked, match.start())
        if newline != -1:
            line_start = newline + 1
        line_checked = match.start()
        if lead_line != line_start:
            lead_line = line_start
            lead_end = match_end(LINE_LEAD, text, line_start)
        span = value_span(text, match.end(), match.group("sep"), match.start("name") == lead_end)
        if span is None:
            continue
        pieces.append(text[copied : span[0]])
        pieces.append(REDACTED)
        copied = scan = span[1]
    pieces.append(text[copied:])
    return "".join(pieces)


def mask_xml(match: re.Match[str]) -> str:
    if not match.group("v").strip():
        return match.group(0)
    return f"{match.group('open')}{REDACTED}{match.group('close')}"


def redact_shapes(text: str) -> str:
    """Layer 1: mask values by shape, keeping quotes, separators and structure."""
    text = mask_key_values(text)
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
