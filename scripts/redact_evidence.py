"""Redact a capture on stdin before it is written to docs/evidence.

Every capture for docs/evidence passes through this filter first:

    some-command 2>&1 | uv run python scripts/redact_evidence.py > capture.txt

The pipeline is three ordered layers, so a secret that contains a path fragment is masked
whole before any path is rewritten:

1. Shapes: eight passes, always in this order: PEM private key, auth header, bearer token,
   credential-named field (JSON, YAML, properties, KEY=value, dict repr, `=>`), secret CLI
   flag, DSN password, XML credential element, JWT-shaped string. The short flags `-u`,
   `--user` and `-p` are not secret names: they are masked only after the HTTP client word or
   after a docker, podman or nerdctl login, on the same line or on a line continued from it by
   a trailing backslash. A non-secret pair never
   hides a credential that follows it (`user=bob&password=X` masks X), and a secret value is
   masked whole, however many words, separators or escaped quotes it holds. A credential name
   also covers dbt's DBT_ENV_SECRET_ prefix and a last name segment of pass or pwd, and a
   single-quoted value may hold a doubled quote (`'it''s'`). SHAPES is the one
   rule the evidence gate reuses through `shape_findings`, so the gate and the redactor can't
   drift apart. No pass adds or removes a line. Every pass is linear in its input, because each
   pattern starts a match only at the beginning of a run of its own characters.
2. Literals: every value in infra/.env of length 8 or more, longest first. It runs only
   when that file exists.
3. Paths: the repo root becomes `<repo>` and the home directory becomes `<home>`.

The mask is the literal `REDACTED`, the same convention as docs/evidence/w1-prove.md. The
output is idempotent. No value is ever printed on an error.
"""

from __future__ import annotations

import argparse
import functools
import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
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
# dbt scrubs every DBT_ENV_SECRET_ variable from its own logs, so the prefix marks a secret
# whatever the tail is. Compared with separators removed, and checked before the exclusions.
SECRET_NAME_PREFIXES = ("dbtenvsecret",)
# A name whose LAST segment is one of these is a credential (`DB_PASS`, `dbPass`, `MYSQL_PWD`).
# They are not SECRET_SUFFIXES, because an ending match would also mask `bypass` and `compass`.
CREDENTIAL_SEGMENTS = frozenset({"pass", "pwd"})
NAME_SEGMENT_SPLIT = re.compile(r"[_.\-]|(?<=[a-z0-9])(?=[A-Z])")
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
# A doubled quote is the YAML and SQL escape. The repetition is possessive, so an unterminated
# value that holds a `''` cannot backtrack to end at that pair: it fails and the caller masks
# to the end of the line.
SINGLE_QUOTED = re.compile(r"'((?:[^'\\\r\n]|\\.|'')*+)'")
TO_LINE_END = re.compile(r"[^\r\n]*")
TO_QUOTE_OR_LINE_END = re.compile(r"""[^\r\n"']*""")
# `&` or `;` that opens another `name=` pair: where a line-level `KEY=value` value stops.
PAIR_JOIN = re.compile(r"[&;](?=[A-Za-z0-9_.\-]+=)")
TO_TERMINATOR = re.compile(r"""[^\s&;,"'}\])]*""")
EMPTY_TERMINATORS = "&;,}])"
DSN = re.compile(
    r"""
    (?<![a-zA-Z0-9+.\-])
    (?P<pre>[a-zA-Z][a-zA-Z0-9+.\-]*://[^\s:/@]*:)
    (?P<pw>[^\s/]+)
    (?=@[^@\s/"']*(?:[/?\#\s"',;)\]}]|$))
    """,
    re.VERBOSE | re.MULTILINE,
)
XML = re.compile(r"(?P<open><(?P<tag>[A-Za-z]+)>)(?P<v>[^<]+)(?P<close></(?P=tag)>)")
XML_SECRET_TAG = re.compile(r"Secret|Token|Password|Credential|AccessKey")
HEADER = re.compile(
    r"""
    (?<![A-Za-z0-9_\-])
    (?P<pre>
        (?P<q>["'])?
        (?:proxy-authorization|authorization|set-cookie|cookie
           |x-amz-security-token|x-amz-signature|x-api-key)
        [ \t]*:[ \t]*
    )
    (?(q)(?P<qv>(?:(?!(?P=q))[^\r\n])*)|(?P<v>[^\r\n]*))
    """,
    re.VERBOSE | re.IGNORECASE,
)
BEARER = re.compile(r"(?<![A-Za-z0-9_\-])(?P<pre>Bearer[ \t]+)(?P<token>[A-Za-z0-9._~+/=\-]{8,})")
# A flag match starts only at the beginning of a run of flag characters, the flag body's own
# class, so each run is scanned a bounded number of times (as NAME_SEP does).
FLAG_START = r"(?<![A-Za-z0-9_.\-])"
# The run before its first flag, when the run does not begin with one (`...--password`,
# `e.g.--token`). It is lazy and atomic: it stops at the first `.` that a flag follows and never
# backtracks, so the flag it leaves is the one an unbounded scan of the run would judge first.
FLAG_LEAD = r"(?P<lead>(?:(?!--?[A-Za-z])(?>[A-Za-z0-9_.\-]*?\.(?=--?[A-Za-z])))?)"
FLAG_VALUE = r"""(?:"(?:[^"\\\r\n]|\\.)*"|'(?:[^'\r\n]|'')*+'|["'][^\r\n]*|[^\s\-]\S*)"""
FLAG = re.compile(
    FLAG_START
    + FLAG_LEAD
    + r"(?P<flag>--?[A-Za-z][A-Za-z0-9_.\-]*)(?P<gap>[ \t]+)(?P<value>"
    + FLAG_VALUE
    + ")"
)
PEM = re.compile(
    r"""
    (?P<begin>-----BEGIN[ ](?P<label>(?:[A-Z]+[ ])*PRIVATE[ ]KEY(?:[ ]BLOCK)?)-----)
    (?P<body>.*?)
    (?P<end>-----END[ ](?P=label)-----|\Z)
    """,
    re.VERBOSE | re.DOTALL,
)
# The command words that give the short flags below their meaning. The context is found once per
# line, never per flag match, so a long line of flags stays linear. `-U` and `-P` never qualify.
CURL_COMMAND = re.compile(r"(?<![\w.\-])curl(?![\w.\-])")
LOGIN_COMMAND = re.compile(r"(?<![\w.\-])(?:docker|podman|nerdctl)[ \t]+login(?![\w.\-])")
CLIENT_USER_FLAGS = frozenset({"-u", "--user"})
LOGIN_SECRET_FLAGS = frozenset({"-p"})
LINE_BREAK = re.compile(r"(\r\n|\n|\r)")
# A JWT match starts only at the beginning of a run of its own characters, so a long run is
# scanned once. The lead keeps a JWT glued after a dash (`id-eyJ...`) masked: it is the run
# before the first `-eyJ`, and is empty when the run begins with `eyJ`.
JWT = re.compile(
    r"""
    (?<![A-Za-z0-9_\-])
    (?P<lead>(?:(?!eyJ)(?>[A-Za-z0-9_\-]*?-(?=eyJ)))?)
    eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]*
    """,
    re.VERBOSE,
)


def is_secret_name(name: str) -> bool:
    """True when a key name carries a credential, so its value must be masked.

    Three rules, in this order. The dbt secret prefix wins over everything, the
    `key` exclusions included. Then a name that ends in a credential word is secret unless it
    is `key` or a non-secret key. Last, a name whose last segment (split at `_`, `.`, `-` and a
    camelCase boundary) is `pass` or `pwd` is secret.
    """
    bare = re.sub(r"[_.\-]", "", name.lower())
    if bare.startswith(SECRET_NAME_PREFIXES):
        return True
    if bare == "key" or bare.endswith(NON_SECRET_KEY_SUFFIXES):
        return False
    if bare.endswith(SECRET_SUFFIXES):
        return True
    return NAME_SEGMENT_SPLIT.split(name)[-1].lower() in CREDENTIAL_SEGMENTS


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


def mask_lines(body: str) -> str:
    """Replace the content of every non-blank line with REDACTED, keeping each line ending."""
    parts = LINE_BREAK.split(body)
    for index in range(0, len(parts), 2):
        line = parts[index]
        content = line.strip()
        if content:
            lead = line[: len(line) - len(line.lstrip())]
            trail = line[len(line.rstrip()) :]
            parts[index] = f"{lead}{REDACTED}{trail}"
    return "".join(parts)


def mask_pem(text: str) -> str:
    """Mask every body line of a PEM private key, from its BEGIN line to its END line or the end."""
    return PEM.sub(lambda m: f"{m['begin']}{mask_lines(m['body'])}{m['end']}", text)


def header_replacement(match: re.Match[str]) -> str:
    value = match["qv"] if match["q"] else match["v"]
    content = value.rstrip(" \t")
    if not content.strip():
        return match[0]
    return f"{match['pre']}{REDACTED}{value[len(content) :]}"


def mask_headers(text: str) -> str:
    """Mask the whole value of an auth or cookie header, wherever it sits on a line."""
    return HEADER.sub(header_replacement, text)


def mask_bearer(text: str) -> str:
    """Mask a free-standing `Bearer <token>`, keeping the word Bearer."""
    return BEARER.sub(lambda m: f"{m['pre']}{REDACTED}", text)


def command_end(pattern: re.Pattern[str], line: str) -> int | None:
    """The end offset of the first match of `pattern` in `line`, or None when it is absent."""
    found = pattern.search(line)
    return found.end() if found is not None else None


def flag_replacement(match: re.Match[str], curl_end: int | None, login_end: int | None) -> str:
    """The masked form of one `flag value` match, or the match itself when it is not a secret.

    A masked result keeps the match's lead, the text before its flag. `curl_end` and `login_end`
    are where the HTTP client word or the registry login words end on this line (0 when the
    command began on an earlier, backslash-continued line), or None when the line has no such
    command. The short flags `-u`, `--user` and `-p` are secret only when they come after that
    command; every other flag is judged by its name.
    """
    lead = match["lead"]
    flag = match["flag"]
    value = match["value"]
    start = match.start("flag")
    quote = value[0] if value[0] in "\"'" else ""
    closing = quote if len(value) > 1 and value.endswith(quote) else ""
    if flag in CLIENT_USER_FLAGS and curl_end is not None and start >= curl_end:
        user, colon, password = value[len(quote) : len(value) - len(closing)].partition(":")
        if not colon or not password.strip():
            return match[0]
        return f"{lead}{flag}{match['gap']}{quote}{user}:{REDACTED}{closing}"
    if flag in LOGIN_SECRET_FLAGS and login_end is not None and start >= login_end:
        return f"{lead}{flag}{match['gap']}{quote}{REDACTED}{closing}"
    name = flag.lstrip("-")
    if name.startswith("no-") or not is_secret_name(name):
        return match[0]
    return f"{lead}{flag}{match['gap']}{quote}{REDACTED}{closing}"


def mask_flags(text: str) -> str:
    """Mask the value of a space-separated secret flag (`--password X`, `--token "a b"`).

    Also masks the password of `-u` / `--user` after the HTTP client word and the value of `-p`
    after a registry login. The command context is computed once per line and carried to the
    next line when this line ends in a backslash. Lines are split and rejoined on `\\n` only,
    so the text keeps its line structure byte for byte.
    """
    lines = text.split("\n")
    carry_client = carry_login = False
    for index, line in enumerate(lines):
        curl_end = 0 if carry_client else command_end(CURL_COMMAND, line)
        login_end = 0 if carry_login else command_end(LOGIN_COMMAND, line)
        replace = functools.partial(flag_replacement, curl_end=curl_end, login_end=login_end)
        lines[index] = FLAG.sub(replace, line)
        continued = line.removesuffix("\r").endswith("\\")
        carry_client = curl_end is not None and continued
        carry_login = login_end is not None and continued
    return "\n".join(lines)


def mask_dsn(text: str) -> str:
    """Mask a DSN password, which ends at the last `@` before the host."""
    return DSN.sub(lambda m: f"{m['pre']}{REDACTED}", text)


def xml_replacement(match: re.Match[str]) -> str:
    if XML_SECRET_TAG.search(match["tag"]) is None:
        return match[0]
    return f"{match['open']}{mask_lines(match['v'])}{match['close']}"


def mask_xml_elements(text: str) -> str:
    """Mask the text of an STS-style credential element, keeping its line structure."""
    return XML.sub(xml_replacement, text)


def mask_jwt(text: str) -> str:
    return JWT.sub(lambda m: f"{m['lead']}{REDACTED}", text)


# Layer 1, in the one order it runs. The reason is the text the evidence gate prints.
SHAPES: tuple[tuple[str, Callable[[str], str]], ...] = (
    ("PEM private key", mask_pem),
    ("auth header", mask_headers),
    ("bearer token", mask_bearer),
    ("credential-named field", mask_key_values),
    ("secret CLI flag", mask_flags),
    ("DSN password", mask_dsn),
    ("XML credential element", mask_xml_elements),
    ("JWT-shaped string", mask_jwt),
)


def redact_shapes(text: str) -> str:
    """Layer 1: mask values by shape, keeping quotes, separators and structure."""
    for _reason, mask in SHAPES:
        text = mask(text)
    return text


def shape_findings(text: str) -> list[tuple[int, str]]:
    """The 1-based line number and reason for every line a Layer 1 pass would change.

    The passes run in SHAPES order on a running copy of the text, exactly as `redact_shapes`
    does. The result is sorted by line, keeping SHAPES order within a line. A pass that adds or
    removes a line raises ValueError naming the pass, never a value.
    """
    findings: list[tuple[int, str]] = []
    current = text
    for reason, mask in SHAPES:
        masked = mask(current)
        if masked.count("\n") != current.count("\n"):
            raise ValueError(f"shape pass {reason} changed the line count")
        if masked != current:
            pairs = zip(current.split("\n"), masked.split("\n"), strict=True)
            findings.extend(
                (number, reason)
                for number, (before, after) in enumerate(pairs, start=1)
                if before != after
            )
        current = masked
    findings.sort(key=lambda finding: finding[0])
    return findings


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
