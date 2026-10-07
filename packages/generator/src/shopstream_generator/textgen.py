"""Word lists and the text renderers: every generated string comes from here (D-18).

Names, product text and categories come only from the committed word lists under
`data/words/`, picked by integer index, so the engine's state holds indices and never text.
`WORD_LISTS` is the only way a resource is named: no code lists the folder, and a name outside
the tuple is refused before any path is built.

A list file is ASCII, one entry per line, LF line endings, one trailing newline, with no blank
line, no repeated entry and no padding. `parse_word_list` fails closed on each and names the
rule (`non-ascii`, `crlf`, `empty`, `missing-final-newline`, `blank-line`, `padded`,
`duplicate`) and the file, never an entry.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files

from .countries import ReferenceDataError

WORD_LISTS = ("first_names", "last_names")


def parse_word_list(name: str, data: bytes) -> tuple[str, ...]:
    """The entries of one list file's bytes, or ReferenceDataError naming the broken rule."""

    def fail(rule: str) -> ReferenceDataError:
        return ReferenceDataError(f"{name}: {rule}")

    if b"\r" in data:
        raise fail("crlf line endings are not allowed")
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError:
        raise fail("non-ascii bytes are not allowed") from None
    if not text:
        raise fail("the list is empty")
    if not text.endswith("\n"):
        raise fail("missing-final-newline")
    entries = text[:-1].split("\n")
    if any(not entry for entry in entries):
        raise fail("blank-line")
    if any(entry != entry.strip() for entry in entries):
        raise fail("padded entry")
    if len(set(entries)) != len(entries):
        raise fail("duplicate entry")
    return tuple(entries)


@cache
def words(name: str) -> tuple[str, ...]:
    """One committed word list, validated once per process."""
    if name not in WORD_LISTS:
        raise ReferenceDataError(f"{name}: not a word list")
    resource = f"{name}.txt"
    try:
        data = files("shopstream_generator").joinpath("data", "words", resource).read_bytes()
    except OSError as exc:
        raise ReferenceDataError(f"{resource}: cannot be read ({type(exc).__name__})") from exc
    return parse_word_list(resource, data)


def full_name(first_idx: int, last_idx: int) -> str:
    """`First Last` from the two name lists."""
    return f"{words('first_names')[first_idx]} {words('last_names')[last_idx]}"


def email(first_idx: int, last_idx: int, customer_id: int, version: int) -> str:
    """`first.last.<id>@example.com` in lower case, with `.<version>` after the id from version 1.

    The domain is the reserved example.com (RFC 2606), so no address can reach a real mailbox.
    """
    local = f"{words('first_names')[first_idx]}.{words('last_names')[last_idx]}.{customer_id}"
    if version:
        local = f"{local}.{version}"
    return f"{local.lower()}@example.com"


def product_name(adjective_idx: int, noun_idx: int) -> str:
    raise NotImplementedError


def category(idx: int) -> str:
    raise NotImplementedError
