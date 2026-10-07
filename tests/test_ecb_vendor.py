"""Unit tests for the one-off ECB vendoring helper.

The helper is stdlib-only and its `build` is pure: every case here feeds synthetic rows (the
shape of Frankfurter's v2 range response) and reads the bytes back, so no test needs Docker, the
Frankfurter volume or the network. `fetch` is the one container-side step; it is tested only with
`urlopen` and `fx_load.providers_check` replaced.
"""

from __future__ import annotations

import hashlib
import io
import itertools
import json
import urllib.request
from datetime import date
from decimal import Decimal
from pathlib import Path

import ecb_vendor
import fx_load
import pytest

# 29 synthetic codes plus EUR make the latest publication's 30 quotes, as the real data does.
CODES = ["".join(pair) for pair in itertools.islice(itertools.product("ABC", "DEF", "GHIJ"), 29)]
D1, D2, D3 = "2025-01-02", "2025-01-03", "2025-01-06"


def row(day: str, quote: str, rate: str, base: str = "EUR") -> dict[str, str]:
    return {"date": day, "base": base, "quote": quote, "rate": rate}


def day_rows(day: str, rate: str = "1.2500", *, bgn: str | None = None) -> list[dict[str, str]]:
    rows = [row(day, code, rate) for code in CODES]
    rows.append(row(day, "EUR", "1.0"))
    if bgn is not None:
        rows.append(row(day, "BGN", bgn))
    return rows


def three_days() -> list[dict[str, str]]:
    """Three publications; BGN is quoted on the first two only."""
    return [*day_rows(D1, bgn="1.9558"), *day_rows(D2, bgn="1.9558"), *day_rows(D3, "1.2600")]


def split(csv_bytes: bytes) -> tuple[list[str], bytes]:
    """The `# key: value` comment lines and the body bytes after them."""
    lines = csv_bytes.split(b"\n")
    comments = [line.decode() for line in lines if line.startswith(b"#")]
    body_start = sum(len(line) + 1 for line in lines if line.startswith(b"#"))
    return comments, csv_bytes[body_start:]


def rows_body(rows: list[dict[str, str]]) -> bytes:
    return json.dumps(rows).encode()


# parse_rates


def test_parse_rates_keeps_the_text_as_served() -> None:
    body = b'[{"date":"2025-01-02","base":"EUR","quote":"USD","rate":0.8512},'
    body += b'{"date":"2025-01-02","base":"EUR","quote":"EUR","rate":1.0},'
    body += b'{"date":"2025-01-02","base":"EUR","quote":"JPY","rate":162.5}]'
    rates = [r["rate"] for r in ecb_vendor.parse_rates(body)]
    assert rates == ["0.8512", "1.0", "162.5"]


@pytest.mark.parametrize("rate", ["1e-3", "1E5", "-1.2", ".5", '"abc"', '"1,5"', "null", "true"])
def test_parse_rates_refuses_anything_but_plain_digits(rate: str) -> None:
    body = f'[{{"date":"2025-01-02","base":"EUR","quote":"USD","rate":{rate}}}]'.encode()
    with pytest.raises(ecb_vendor.VendorError):
        ecb_vendor.parse_rates(body)


@pytest.mark.parametrize("missing", ["date", "base", "quote", "rate"])
def test_parse_rates_refuses_a_row_missing_a_field(missing: str) -> None:
    record = {"date": "2025-01-02", "base": "EUR", "quote": "USD", "rate": "1.1"}
    del record[missing]
    with pytest.raises(ecb_vendor.VendorError, match=missing):
        ecb_vendor.parse_rates(rows_body([record]))


def test_parse_rates_refuses_a_body_that_is_not_a_list_of_rows() -> None:
    for body in (b'{"date": "2025-01-02"}', b"[1, 2]", b"not json"):
        with pytest.raises(ecb_vendor.VendorError):
            ecb_vendor.parse_rates(body)


# build: layout, checksums and ordering


def test_build_writes_the_header_then_a_sorted_wide_body() -> None:
    built = ecb_vendor.build(three_days(), date(2026, 10, 7))
    comments, body = split(built.rates_csv)
    keys = [line.split(":", 1)[0] for line in comments]
    assert keys == ["# source", "# fetched", "# rows", "# max_move_bp", "# sha256"]
    assert comments[0] == f"# source: {ecb_vendor.SOURCE}"
    assert comments[1] == "# fetched: 2026-10-07; cut: 2026-10-02"
    assert comments[2] == "# rows: 3 publications x 31 currencies; 92 quoted cells"
    header, *lines = body.decode().split("\n")
    columns = header.split(",")
    assert columns[0] == "date"
    assert columns[1:] == sorted([*CODES, "BGN", "EUR"])
    assert [line.split(",")[0] for line in lines[:-1]] == [D1, D2, D3]
    assert lines[-1] == ""
    assert built.rates_csv.endswith(b"\n")
    assert not built.rates_csv.endswith(b"\n\n")
    assert b"\r" not in built.rates_csv


def test_an_unquoted_currency_is_an_empty_cell() -> None:
    built = ecb_vendor.build(three_days(), date(2026, 10, 7))
    _, body = split(built.rates_csv)
    header, *lines = body.decode().split("\n")
    bgn = header.split(",").index("BGN")
    cells = [line.split(",")[bgn] for line in lines[:-1]]
    assert cells == ["1.9558", "1.9558", ""]


def test_the_csv_checksum_covers_the_bytes_after_the_comment_block() -> None:
    built = ecb_vendor.build(three_days(), date(2026, 10, 7))
    comments, body = split(built.rates_csv)
    recorded = comments[-1].removeprefix("# sha256: ")
    assert recorded == hashlib.sha256(body).hexdigest()


def test_the_json_checksum_covers_the_canonical_quotes() -> None:
    built = ecb_vendor.build(three_days(), date(2026, 10, 7))
    document = json.loads(built.latest_json)
    canonical = json.dumps(
        document["quotes"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    assert document["meta"]["sha256"] == hashlib.sha256(canonical.encode()).hexdigest()
    assert document["meta"]["count"] == "30"
    assert document["meta"]["date"] == D3
    assert document["meta"]["cut"] == "2026-10-02"
    assert document["meta"]["fetched"] == "2026-10-07"
    assert document["quotes"]["EUR"] == "1.0"
    assert "BGN" not in document["quotes"]
    assert built.latest_json.endswith(b"\n")
    assert not built.latest_json.endswith(b"\n\n")


def test_build_gives_the_same_bytes_for_any_row_order() -> None:
    rows = three_days()
    first = ecb_vendor.build(rows, date(2026, 10, 7))
    assert first.rates_csv
    assert first.latest_json
    for reordered in (rows[::-1], rows[::2] + rows[1::2], rows[17:] + rows[:17]):
        second = ecb_vendor.build(reordered, date(2026, 10, 7))
        assert first.rates_csv == second.rates_csv
        assert first.latest_json == second.latest_json


def test_stats_hold_counts_and_the_max_move() -> None:
    stats = ecb_vendor.build(three_days(), date(2026, 10, 7)).stats
    assert stats["publications"] == 3
    assert stats["columns"] == 31
    assert stats["cells"] == 92
    assert stats["latest_date"] == D3
    assert stats["latest_count"] == 30
    assert stats["max_move_bp"] == "80.00"
    assert stats["max_move_currency"] == CODES[0]
    assert stats["max_move_date"] == D3


# build: refusals


def test_rows_after_the_cut_are_dropped_first() -> None:
    late = day_rows("2026-10-05", "9.9999")
    built = ecb_vendor.build([*three_days(), *late], date(2026, 10, 7))
    assert json.loads(built.latest_json)["meta"]["date"] == D3
    assert b"2026-10-05" not in built.rates_csv


def test_a_row_dated_on_the_cut_is_kept() -> None:
    built = ecb_vendor.build(day_rows("2026-10-02"), date(2026, 10, 7))
    assert json.loads(built.latest_json)["meta"]["date"] == "2026-10-02"


def test_a_base_other_than_eur_is_refused() -> None:
    rows = [*three_days(), row(D2, "USD", "1.1", base="USD")]
    with pytest.raises(ecb_vendor.VendorError, match="base"):
        ecb_vendor.build(rows, date(2026, 10, 7))


@pytest.mark.parametrize("rate", ["0", "0.0", "0.0000"])
def test_a_non_positive_rate_is_refused(rate: str) -> None:
    rows = three_days()
    rows[0] = row(D1, CODES[0], rate)
    with pytest.raises(ecb_vendor.VendorError, match="positive"):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_two_different_rates_for_one_date_and_currency_are_refused() -> None:
    rows = [*three_days(), row(D2, CODES[0], "1.2501")]
    with pytest.raises(ecb_vendor.VendorError, match="conflict"):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_a_repeated_identical_row_is_harmless() -> None:
    once = ecb_vendor.build(three_days(), date(2026, 10, 7))
    twice = ecb_vendor.build([*three_days(), row(D2, CODES[0], "1.2500")], date(2026, 10, 7))
    assert once.rates_csv
    assert once.rates_csv == twice.rates_csv


def test_the_latest_publication_must_hold_exactly_thirty_quotes() -> None:
    rows = [r for r in three_days() if not (r["date"] == D3 and r["quote"] == CODES[0])]
    with pytest.raises(ecb_vendor.VendorError, match="30"):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_the_latest_publication_must_exclude_bgn() -> None:
    rows = [r for r in three_days() if not (r["date"] == D3 and r["quote"] == CODES[0])]
    rows.append(row(D3, "BGN", "1.9558"))
    with pytest.raises(ecb_vendor.VendorError, match="BGN"):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_the_latest_publication_must_include_eur() -> None:
    rows = [r for r in three_days() if not (r["date"] == D3 and r["quote"] == "EUR")]
    rows.append(row(D3, "BGN", "1.9558"))
    with pytest.raises(ecb_vendor.VendorError):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_no_rows_at_all_is_refused() -> None:
    with pytest.raises(ecb_vendor.VendorError):
        ecb_vendor.build([], date(2026, 10, 7))


# max_move and the 10 % bound


def test_max_move_names_the_currency_and_the_later_date() -> None:
    cells = {D1: {"USD": "1.0000", "JPY": "160.0"}, D2: {"USD": "1.0531", "JPY": "160.1"}}
    move, code, day = ecb_vendor.max_move([D1, D2], ["JPY", "USD"], cells)
    assert (move, code, day) == (Decimal("531.00"), "USD", D2)


def test_max_move_is_the_absolute_value_of_a_fall() -> None:
    cells = {D1: {"USD": "1.0000"}, D2: {"USD": "0.9469"}}
    move, _, _ = ecb_vendor.max_move([D1, D2], ["USD"], cells)
    assert move == Decimal("531.00")


def test_max_move_rounds_half_up_to_two_places_in_decimal() -> None:
    cells = {D1: {"USD": "3"}, D2: {"USD": "3.001"}}  # 3.33333... bp
    move, _, _ = ecb_vendor.max_move([D1, D2], ["USD"], cells)
    assert move == Decimal("3.33")
    tie = {D1: {"USD": "2"}, D2: {"USD": "2.000001"}}  # exactly 0.005 bp
    assert ecb_vendor.max_move([D1, D2], ["USD"], tie)[0] == Decimal("0.01")


def test_a_move_is_measured_only_between_consecutive_quotes_of_one_currency() -> None:
    cells = {
        D1: {"BGN": "1.0", "USD": "1.0000"},
        D2: {"USD": "1.0001"},
        D3: {"BGN": "9.0", "USD": "1.0001"},
    }
    found = ecb_vendor.max_move([D1, D2, D3], ["BGN", "USD"], cells)
    assert found == (Decimal("1.00"), "USD", D2)


def test_a_move_of_exactly_1000_bp_builds() -> None:
    rows = [*day_rows(D1, "1.0000"), *day_rows(D2, "1.1000")]
    built = ecb_vendor.build(rows, date(2026, 10, 7))
    assert built.stats["max_move_bp"] == "1000.00"
    assert "# max_move_bp: 1000.00 (" in built.rates_csv.decode()


def test_a_move_of_1000_01_bp_is_refused_with_the_measurement() -> None:
    rows = [*day_rows(D1, "1.000000"), *day_rows(D2, "1.100001")]
    with pytest.raises(ecb_vendor.VendorError, match=r"1000\.01"):
        ecb_vendor.build(rows, date(2026, 10, 7))


def test_the_bound_is_one_tenth_of_a_rate() -> None:
    assert Decimal(1000) == ecb_vendor.MAX_MOVE_BP
    assert ecb_vendor.EXPECTED_LATEST == 30
    assert date(2024, 12, 1) == ecb_vendor.FIRST
    assert date(2026, 10, 2) == ecb_vendor.CUT


# main


def test_main_build_writes_both_files_and_prints_the_stats(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "raw.ndjson"
    raw.write_text("".join(json.dumps(r) + "\n" for r in three_days()))
    out = tmp_path / "out"
    out.mkdir()
    argv = ["build", "--raw", str(raw), "--out-dir", str(out), "--fetched", "2026-10-07"]
    assert ecb_vendor.main(argv) == 0
    assert (out / "ecb_rates.csv").read_bytes() == (
        ecb_vendor.build(three_days(), date(2026, 10, 7)).rates_csv
    )
    assert (out / "ecb_latest.json").exists()
    printed = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert printed["publications"] == 3


def test_main_build_reports_a_refusal_on_stderr_and_returns_one(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    raw = tmp_path / "raw.ndjson"
    raw.write_text(json.dumps(row(D1, "USD", "0")) + "\n")
    argv = ["build", "--raw", str(raw), "--out-dir", str(tmp_path), "--fetched", "2026-10-07"]
    assert ecb_vendor.main(argv) == 1
    assert capsys.readouterr().err.strip() != ""
    assert not (tmp_path / "ecb_rates.csv").exists()


# fetch


class FakeResponse(io.BytesIO):
    def __enter__(self) -> FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def test_fetch_refuses_an_incomplete_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[date | None] = []

    def incomplete(today: date | None = None) -> dict[str, object]:
        seen.append(today)
        return {"complete": False, "reason": "publishes_missed is 3"}

    monkeypatch.setattr(fx_load, "providers_check", incomplete)
    with pytest.raises(ecb_vendor.VendorError, match="publishes_missed"):
        ecb_vendor.fetch()
    assert seen == [ecb_vendor.CUT]


def test_fetch_asks_one_range_per_year_and_keeps_only_rows_inside_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(fx_load, "providers_check", lambda today=None: {"complete": True})
    urls: list[str] = []

    def fake_urlopen(url: str, timeout: float) -> FakeResponse:
        urls.append(url)
        carried_in = row("2024-11-29", "USD", "1.0563")  # the previous publication
        inside = row(url.split("from=")[1][:10], "USD", "1.05")
        return FakeResponse(rows_body([carried_in, inside]))

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    fetched = ecb_vendor.fetch()
    assert [u.split("?", 1)[1] for u in urls] == [
        "providers=ECB&from=2024-12-01&to=2024-12-31",
        "providers=ECB&from=2025-01-01&to=2025-12-31",
        "providers=ECB&from=2026-01-01&to=2026-10-02",
    ]
    assert all(u.startswith("http://frankfurter:8080/v2/rates?") for u in urls)
    assert [r["date"] for r in fetched] == ["2024-12-01", "2025-01-01", "2026-01-01"]
