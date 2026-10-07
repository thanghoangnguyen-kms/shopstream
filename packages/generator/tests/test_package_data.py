"""The committed word lists and country table are package data read through importlib.resources (D-18).

One validator, `textgen.parse_word_list`, guards every word list: it fails closed on the shapes
that would make two machines or two editors disagree about a list (a non-ASCII byte, a blank
line, a repeat, CRLF, a padded entry, a missing final newline). Each rule is proved by a sample
held in memory, so the committed files are never edited to see a rule fire. A failure names the
file and the rule, never an entry.
"""

from __future__ import annotations

import re
from importlib.resources import files

import pytest
from shopstream_generator import countries, textgen
from shopstream_generator.countries import ReferenceDataError

WORDS = files("shopstream_generator").joinpath("data", "words")
NAME_ONLY_LETTERS = re.compile(r"[A-Z][a-z]+")

BAD_SAMPLES = [
    pytest.param(b"Ann\nCaf\xc3\xa9\n", "non-ascii", id="non-ascii"),
    pytest.param(b"Ann\n\xff\n", "non-ascii", id="not-utf8"),
    pytest.param(b"Ann\n\nBob\n", "blank-line", id="blank-line"),
    pytest.param(b"Ann\nBob\n\n", "blank-line", id="trailing-blank-line"),
    pytest.param(b"Ann\nBob\nAnn\n", "duplicate", id="duplicate"),
    pytest.param(b"Ann\r\nBob\r\n", "crlf", id="crlf"),
    pytest.param(b" Ann\nBob\n", "padded", id="padded-left"),
    pytest.param(b"Ann\nBob \n", "padded", id="padded-right"),
    pytest.param(b"Ann\nBob", "missing-final-newline", id="missing-final-newline"),
    pytest.param(b"", "empty", id="empty"),
]


@pytest.mark.parametrize(("sample", "rule"), BAD_SAMPLES)
def test_the_word_list_validator_rejects_each_bad_shape(sample: bytes, rule: str) -> None:
    with pytest.raises(ReferenceDataError, match=rule):
        textgen.parse_word_list("sample.txt", sample)


def test_the_validator_accepts_a_clean_list() -> None:
    assert textgen.parse_word_list("sample.txt", b"Ann\nBob\n") == ("Ann", "Bob")


@pytest.mark.parametrize("name", textgen.WORD_LISTS)
def test_every_named_word_list_loads_through_importlib_resources(name: str) -> None:
    data = WORDS.joinpath(f"{name}.txt").read_bytes()
    assert textgen.words(name) == textgen.parse_word_list(f"{name}.txt", data)
    assert len(textgen.words(name)) >= 1


def test_the_word_list_folder_holds_exactly_the_named_lists() -> None:
    present = sorted(entry.name for entry in WORDS.iterdir())
    assert present == sorted(f"{name}.txt" for name in textgen.WORD_LISTS)


def test_an_unnamed_word_list_is_refused() -> None:
    with pytest.raises(ReferenceDataError, match="not a word list"):
        textgen.words("../countries")


@pytest.mark.parametrize("name", ["first_names", "last_names"])
def test_the_name_lists_are_capitalised_letters_only_and_long_enough(name: str) -> None:
    entries = textgen.words(name)
    assert len(entries) >= 40
    assert all(NAME_ONLY_LETTERS.fullmatch(entry) for entry in entries)


def test_the_country_table_loads_through_importlib_resources() -> None:
    data = files("shopstream_generator").joinpath("data", "countries.csv").read_bytes()
    assert data.startswith(b"country,currency,weight,cities\n")
    assert countries.countries() == countries.parse_countries(data.decode("ascii"))
    assert len(countries.country_weights()) == len(countries.countries())
