"""The country table: which currency a customer's country pays in, how often, and its cities (D-12).

`data/countries.csv` is package data read through `importlib.resources`, so it never depends on
a path outside the package. Its header is `country,currency,weight,cities`; rows are sorted by
ISO 3166-1 alpha-2 code, the weight is a positive integer, and `cities` is a `|`-joined list of
ASCII place names, unique within the row. Row order is the draw order: a country is drawn with
`Stream.choice_index` over the weights in file order and a city with `Stream.pick` over the
row's city order, so nothing here depends on set or dict iteration.

The reader fails closed: a file that is not exactly this shape raises `ReferenceDataError`
naming the rule (and the row number), never a cell's text. Whether a currency is one the ECB
quotes is the test suite's check against `fx.latest_quotes()`, not the reader's.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass
from functools import cache
from importlib.resources import files

HEADER = ("country", "currency", "weight", "cities")
_CODE = re.compile(r"[A-Z]{2}")
_CURRENCY = re.compile(r"[A-Z]{3}")
_WEIGHT = re.compile(r"[1-9][0-9]*")
_CITY_SEPARATOR = "|"


class ReferenceDataError(ValueError):
    """A committed reference file is missing or not in the shape the generator accepts."""


@dataclass(frozen=True)
class Country:
    """One table row: the country's code, its currency, its draw weight and its cities."""

    code: str
    currency: str
    weight: int
    cities: tuple[str, ...]


def _cities(field: str, row: int) -> tuple[str, ...]:
    cities = tuple(field.split(_CITY_SEPARATOR))
    if (
        not field
        or any(not city or city != city.strip() or not city.isascii() for city in cities)
        or len(set(cities)) != len(cities)
    ):
        raise ReferenceDataError(
            f"countries.csv row {row}: cities must be unique, ASCII, unpadded and non-empty"
        )
    return cities


def parse_countries(text: str) -> tuple[Country, ...]:
    """Read the table text, or raise ReferenceDataError naming the rule that failed."""
    if "\r" in text:
        raise ReferenceDataError("countries.csv: crlf line endings are not allowed")
    if not text.endswith("\n"):
        raise ReferenceDataError("countries.csv: missing-final-newline")
    rows = list(csv.reader(io.StringIO(text, newline="")))
    if not rows or tuple(rows[0]) != HEADER:
        raise ReferenceDataError(f"countries.csv: the header must be {','.join(HEADER)}")
    if len(rows) == 1:
        raise ReferenceDataError("countries.csv: the table is empty")
    table: list[Country] = []
    for number, row in enumerate(rows[1:], start=2):
        if len(row) != len(HEADER):
            raise ReferenceDataError(f"countries.csv row {number}: columns, expected {len(HEADER)}")
        code, currency, weight, cities = row
        if not _CODE.fullmatch(code):
            raise ReferenceDataError(f"countries.csv row {number}: code must be two capitals")
        if not _CURRENCY.fullmatch(currency):
            raise ReferenceDataError(f"countries.csv row {number}: currency must be three capitals")
        if not _WEIGHT.fullmatch(weight):
            raise ReferenceDataError(
                f"countries.csv row {number}: weight must be a positive integer"
            )
        table.append(Country(code, currency, int(weight), _cities(cities, number)))
    return tuple(table)


@cache
def countries() -> tuple[Country, ...]:
    """The committed table, in file order, read once per process."""
    try:
        data = files("shopstream_generator").joinpath("data", "countries.csv").read_bytes()
        return parse_countries(data.decode("ascii"))
    except (OSError, UnicodeDecodeError) as exc:
        raise ReferenceDataError(f"countries.csv: cannot be read ({type(exc).__name__})") from exc


def country_weights() -> tuple[int, ...]:
    """The draw weights in file order."""
    return tuple(country.weight for country in countries())
