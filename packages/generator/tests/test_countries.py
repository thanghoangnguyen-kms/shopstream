"""D-12: the country table against the vendored latest quotes and the TARGET calendar.

`table_problems` is the one place that says what a good table is beyond its shape: every
currency is EUR or quoted in the latest publication, Bulgaria and BGN are absent (BGN is quoted
only through 2025-12-31), codes are unique and sorted, and the euro rows hold at least half of
the weight. Each rule is proved by a sample, so the committed table passing means something.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from datetime import date

import pytest
from shopstream_generator import countries, fx
from shopstream_generator.countries import Country, ReferenceDataError

from .target_calendar import target_days

HEADER = "country,currency,weight,cities\n"
FIRST_DAY = date(2025, 1, 1)
LAST_DAY = date(2025, 12, 31)


def table_problems(table: Sequence[Country], quotes: Collection[str]) -> list[str]:
    """The rule ids a parsed table breaks; empty for a good table."""
    found: list[str] = []
    codes = [country.code for country in table]
    if any(country.currency not in quotes for country in table):
        found.append("currency-not-quoted")
    if any(country.currency == "BGN" for country in table):
        found.append("bgn")
    if "BG" in codes:
        found.append("bulgaria")
    if len(codes) != len(set(codes)):
        found.append("duplicate-code")
    if codes != sorted(codes):
        found.append("unsorted")
    total = sum(country.weight for country in table)
    euro = sum(country.weight for country in table if country.currency == "EUR")
    if 2 * euro < total:
        found.append("eur-below-half")
    return found


def parse(*rows: str) -> tuple[Country, ...]:
    return countries.parse_countries(HEADER + "".join(f"{row}\n" for row in rows))


def test_the_committed_table_is_good() -> None:
    assert table_problems(countries.countries(), fx.latest_quotes()) == []


def test_every_mapped_currency_has_a_rate_on_every_target_business_day_of_2025() -> None:
    snapshot = fx.snapshot()
    position = {day: index for index, day in enumerate(snapshot.dates)}
    days = target_days(FIRST_DAY, LAST_DAY)
    assert len(days) == 255
    for currency in sorted({country.currency for country in countries.countries()}):
        cells = snapshot.cells[currency]
        missing = [day for day in days if day not in position or cells[position[day]] is None]
        assert missing == [], f"{currency} has no rate on {len(missing)} TARGET days"


def test_codes_are_two_capital_letters_weights_positive_and_cities_clean() -> None:
    for country in countries.countries():
        assert len(country.code) == 2
        assert country.code.isascii()
        assert country.code.isupper()
        assert country.weight >= 1
        assert len(country.cities) >= 1
        assert len(set(country.cities)) == len(country.cities)
        assert all(city.isascii() and city == city.strip() and city for city in country.cities)


def test_bulgaria_and_bgn_are_absent() -> None:
    table = countries.countries()
    assert all(country.code != "BG" for country in table)
    assert all(country.currency != "BGN" for country in table)


def test_the_euro_countries_hold_at_least_half_the_weight() -> None:
    table = countries.countries()
    euro = sum(country.weight for country in table if country.currency == "EUR")
    assert 2 * euro >= sum(country.weight for country in table)
    assert len([country for country in table if country.currency == "EUR"]) == 20


def test_the_weights_follow_file_order_and_sum_positive() -> None:
    table = countries.countries()
    assert countries.country_weights() == tuple(country.weight for country in table)
    assert sum(countries.country_weights()) > 0


BAD_TABLES = [
    pytest.param("AT,,5,Vienna", "currency", id="empty-currency"),
    pytest.param("AT,EUR,0,Vienna", "weight", id="zero-weight"),
    pytest.param("AT,EUR,-3,Vienna", "weight", id="negative-weight"),
    pytest.param("AT,EUR,five,Vienna", "weight", id="non-numeric-weight"),
    pytest.param("AT,EUR,5,", "cities", id="no-city"),
    pytest.param("AT,EUR,5,Vienna||Graz", "cities", id="blank-city"),
    pytest.param("AT,EUR,5,Vienna|Vienna", "cities", id="repeated-city"),
    pytest.param("AT,EUR,5,Wien|Zürich", "cities", id="non-ascii-city"),
    pytest.param("AT,EUR,5, Vienna", "cities", id="padded-city"),
    pytest.param("at,EUR,5,Vienna", "code", id="lowercase-code"),
    pytest.param("AUT,EUR,5,Vienna", "code", id="three-letter-code"),
    pytest.param("AT,eur,5,Vienna", "currency", id="lowercase-currency"),
    pytest.param("AT,EUR,5", "columns", id="missing-column"),
    pytest.param("AT,EUR,5,Vienna,extra", "columns", id="extra-column"),
]


@pytest.mark.parametrize(("row", "rule"), BAD_TABLES)
def test_a_malformed_row_is_refused_naming_the_rule(row: str, rule: str) -> None:
    with pytest.raises(ReferenceDataError, match=rule):
        parse(row)


def test_a_wrong_header_or_an_empty_table_is_refused() -> None:
    with pytest.raises(ReferenceDataError, match="header"):
        countries.parse_countries("code,currency,weight,cities\nAT,EUR,5,Vienna\n")
    with pytest.raises(ReferenceDataError, match="empty"):
        countries.parse_countries(HEADER)


def test_crlf_and_a_missing_final_newline_are_refused() -> None:
    with pytest.raises(ReferenceDataError, match="crlf"):
        countries.parse_countries(HEADER.replace("\n", "\r\n") + "AT,EUR,5,Vienna\r\n")
    with pytest.raises(ReferenceDataError, match="missing-final-newline"):
        countries.parse_countries(HEADER + "AT,EUR,5,Vienna")


QUOTES = {"EUR", "USD", "GBP"}

BAD_RULES = [
    pytest.param(
        ["AT,EUR,5,Vienna", "XX,XYZ,1,Nowhere"], "currency-not-quoted", id="currency-outside-quotes"
    ),
    pytest.param(["AT,EUR,5,Vienna", "BG,BGN,1,Sofia"], "bgn", id="bgn-row"),
    pytest.param(["AT,EUR,5,Vienna", "BG,EUR,1,Sofia"], "bulgaria", id="bulgaria-row"),
    pytest.param(["AT,EUR,5,Vienna", "AT,EUR,1,Graz"], "duplicate-code", id="repeated-code"),
    pytest.param(["US,USD,5,Boston", "AT,EUR,9,Vienna"], "unsorted", id="unsorted"),
    pytest.param(["AT,EUR,4,Vienna", "US,USD,5,Boston"], "eur-below-half", id="eur-below-half"),
]


@pytest.mark.parametrize(("rows", "rule"), BAD_RULES)
def test_table_problems_catches_each_rule(rows: list[str], rule: str) -> None:
    assert rule in table_problems(parse(*rows), QUOTES)


def test_table_problems_is_quiet_for_a_good_sample() -> None:
    assert (
        table_problems(parse("AT,EUR,5,Vienna", "GB,GBP,2,London", "US,USD,1,Boston"), QUOTES) == []
    )
