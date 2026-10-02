"""Unit tests for scripts/semantic_check.py: the MetricFlow output parser, the row comparison and
item 4's verdict rule.

No MetricFlow, DuckDB or subprocess is touched: the fixtures are literal text in the format
`mf query --csv` writes (CRLF line ends, a header row, a midnight timestamp per day).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest
import semantic_check as sc

GROUPED_CSV = (
    "metric_time__day,total_revenue\r\n"
    "2026-09-03 00:00:00,75.0\r\n"
    "2026-09-01 00:00:00,100.0\r\n"
    "2026-09-02 00:00:00,75.0\r\n"
)
TOTAL_CSV = "total_revenue\r\n250.0\r\n"
GOLD_ROWS = [
    ("2026-09-01", Decimal("100.00")),
    ("2026-09-02", Decimal("75.00")),
    ("2026-09-03", Decimal("75.00")),
]
VERSIONS = {sc.DBT_CLI: "1.12.5", sc.MF_DBT_CLI: "1.12.5"}


def matching() -> sc.Comparison:
    return sc.compare_rows(GOLD_ROWS, GOLD_ROWS)


def mismatching() -> sc.Comparison:
    return sc.compare_rows(GOLD_ROWS, GOLD_ROWS[:2])


def verdict(
    *,
    validate_exit: int = 0,
    query_exits: tuple[int, ...] = (0, 0),
    comparisons: tuple[sc.Comparison, ...] | None = None,
    versions: dict[str, str | None] | None = None,
) -> tuple[str, list[str]]:
    return sc.item4_verdict(
        validate_exit=validate_exit,
        query_exits=query_exits,
        comparisons=(matching(), matching()) if comparisons is None else comparisons,
        cli_versions=VERSIONS if versions is None else versions,
    )


# --- the parser --------------------------------------------------------------------------------


def test_the_grouped_csv_parses_into_day_and_value_pairs_in_file_order() -> None:
    assert sc.parse_mf_csv(GROUPED_CSV) == [
        ("2026-09-03 00:00:00", "75.0"),
        ("2026-09-01 00:00:00", "100.0"),
        ("2026-09-02 00:00:00", "75.0"),
    ]


def test_the_ungrouped_csv_parses_into_one_value_with_no_day() -> None:
    assert sc.parse_mf_csv(TOTAL_CSV) == [(None, "250.0")]


def test_a_header_only_csv_has_no_rows() -> None:
    assert sc.parse_mf_csv("metric_time__day,total_revenue\r\n") == []


def test_a_csv_with_three_columns_is_refused() -> None:
    with pytest.raises(ValueError, match="column"):
        sc.parse_mf_csv("a,b,c\r\n1,2,3\r\n")


# --- normalisation -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day",
    [
        "2026-09-01 00:00:00",
        "2026-09-01T00:00:00",
        "2026-09-01",
        date(2026, 9, 1),
        datetime(2026, 9, 1),
    ],
)
def test_a_midnight_timestamp_and_a_date_both_become_the_iso_day(day: object) -> None:
    assert sc.normalise_rows([(day, "100")]) == [("2026-09-01", Decimal(100))]


def test_a_timestamp_after_midnight_is_refused() -> None:
    with pytest.raises(ValueError, match="midnight"):
        sc.normalise_rows([("2026-09-01 06:30:00", "100")])


def test_a_total_keeps_no_day() -> None:
    assert sc.normalise_rows([(None, 250.0)]) == [(None, Decimal(250))]


def test_a_value_that_is_not_a_number_is_refused() -> None:
    with pytest.raises(ValueError, match="number"):
        sc.normalise_rows([("2026-09-01", "abc")])


@pytest.mark.parametrize("value", [250, 250.0, "250", "250.00", Decimal("250.000")])
def test_250_in_any_numeric_form_equals_250_00(value: object) -> None:
    mf = sc.normalise_rows([(None, value)])
    assert sc.compare_rows(mf, [(None, Decimal("250.00"))]).match


def test_75_01_does_not_equal_75() -> None:
    comparison = sc.compare_rows([("2026-09-02", Decimal("75.01"))], [("2026-09-02", Decimal(75))])
    assert not comparison.match
    assert comparison.differing == (("2026-09-02", Decimal("75.01"), Decimal(75)),)


# --- the comparison ----------------------------------------------------------------------------


def test_the_comparison_ignores_row_order() -> None:
    assert sc.compare_rows(list(reversed(GOLD_ROWS)), GOLD_ROWS).match


def test_a_day_only_in_the_sql_is_missing_and_a_day_only_in_metricflow_is_extra() -> None:
    mf = [("2026-09-01", Decimal(100)), ("2026-09-04", Decimal(5))]
    sql = [("2026-09-01", Decimal(100)), ("2026-09-02", Decimal(75))]
    comparison = sc.compare_rows(mf, sql)
    assert not comparison.match
    assert comparison.missing_days == ("2026-09-02",)
    assert comparison.extra_days == ("2026-09-04",)
    assert comparison.differing == ()


def test_a_day_with_no_orders_in_neither_result_still_matches() -> None:
    assert sc.compare_rows(GOLD_ROWS, list(GOLD_ROWS)).missing_days == ()


@pytest.mark.parametrize(
    ("mf", "sql"),
    [([], GOLD_ROWS), (GOLD_ROWS, []), ([], [])],
    ids=["metricflow-empty", "sql-empty", "both-empty"],
)
def test_an_empty_result_on_either_side_is_a_mismatch_never_a_pass(
    mf: list[tuple[str | None, Decimal]], sql: list[tuple[str | None, Decimal]]
) -> None:
    comparison = sc.compare_rows(mf, sql)
    assert comparison.empty
    assert not comparison.match


def test_a_day_that_appears_twice_is_a_mismatch() -> None:
    doubled = [*GOLD_ROWS, ("2026-09-01", Decimal(100))]
    comparison = sc.compare_rows(doubled, GOLD_ROWS)
    assert not comparison.match
    assert comparison.duplicate_days == ("2026-09-01",)


# --- the rest of the pure helpers --------------------------------------------------------------


def test_dbt_version_output_gives_the_installed_core_version() -> None:
    text = "Core:\n  - installed: 1.12.5\n  - latest:    1.12.5 - Up to date!\n\nPlugins:\n"
    assert sc.dbt_core_version(text) == "1.12.5"


def test_dbt_version_output_with_no_installed_line_gives_none() -> None:
    assert sc.dbt_core_version("Traceback (most recent call last):\nImportError: x") is None


def test_the_hand_written_sql_reads_the_gold_relation_and_sums_amount() -> None:
    for sql in (sc.HAND_SQL, sc.HAND_TOTAL_SQL):
        assert "lk.gold.fct_orders" in sql
        assert "SUM(amount)" in sql
    assert "GROUP BY" in sc.HAND_SQL
    assert "GROUP BY" not in sc.HAND_TOTAL_SQL


# --- item 4's verdict --------------------------------------------------------------------------


def test_both_comparisons_matching_with_both_clis_on_1_12_is_go() -> None:
    result, reasons = verdict()
    assert result == "go"
    assert reasons


def test_a_failing_validate_configs_is_fallback() -> None:
    assert verdict(validate_exit=1)[0] == "fallback"


def test_a_failing_query_is_fallback() -> None:
    assert verdict(query_exits=(0, 2))[0] == "fallback"


def test_metricflow_failing_outranks_a_mismatch() -> None:
    assert verdict(query_exits=(1, 1), comparisons=(mismatching(), mismatching()))[0] == "fallback"


def test_a_row_mismatch_is_inconclusive() -> None:
    assert verdict(comparisons=(matching(), mismatching()))[0] == "inconclusive"


def test_no_comparison_at_all_is_inconclusive() -> None:
    assert verdict(comparisons=())[0] == "inconclusive"


def test_an_empty_comparison_is_inconclusive() -> None:
    assert verdict(comparisons=(sc.compare_rows([], []), matching()))[0] == "inconclusive"


@pytest.mark.parametrize(
    "versions",
    [
        {sc.DBT_CLI: "1.12.5"},
        {sc.DBT_CLI: "1.12.5", sc.MF_DBT_CLI: None},
        {sc.DBT_CLI: "1.12.5", sc.MF_DBT_CLI: "1.11.15"},
        {sc.DBT_CLI: "2.0.6", sc.MF_DBT_CLI: "1.12.5"},
    ],
    ids=["cli-absent", "version-unreadable", "mf-cli-on-1.11", "dbt-cli-on-2.0"],
)
def test_a_missing_or_wrong_dbt_cli_version_is_inconclusive(
    versions: dict[str, str | None],
) -> None:
    result, reasons = verdict(versions=versions)
    assert result == "inconclusive"
    assert any("dbt" in reason for reason in reasons)
