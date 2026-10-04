"""Unit tests for scripts/clock_check.py: item 11's pure rules.

No Kafka, Postgres, Karapace or PyIceberg is touched: the changes are hand-built dicts shaped like the
ones `change_from_row` makes from a bronze row, and fake values come from `secrets`.
"""

from __future__ import annotations

import copy
import hashlib
import json
import secrets
from collections.abc import Mapping
from typing import Any

import clock_check as cc
import pytest

EPOCH_2026 = 1767225600000123
COMMIT_LINE = "COMMIT {xid} (at 2026-10-03 10:00:00+00)"


def change(
    pk: int = 1,
    offset: int = 0,
    op: str = "u",
    lsn: int | None = 10,
    txid: int | None = 1,
    snapshot: str | None = "false",
    after_ts: int | None = None,
    before_ts: int | None = None,
    sequence: str | None = None,
    table: str = "customers",
    partition: int = 0,
    **extra: Any,
) -> dict[str, Any]:
    """One bronze change in the shape `change_from_row` returns."""
    return {
        "table": table,
        "pk": (pk,),
        "partition": partition,
        "offset": offset,
        "op": op,
        "lsn": lsn,
        "txid": txid,
        "snapshot": snapshot,
        "after_ts": after_ts,
        "before_ts": before_ts,
        "sequence": sequence,
        "product_id": None,
        "tier_set": False,
        "canary": False,
        "body_sha256": None,
        **extra,
    }


def sequence_text(first: int | None, second: int | None) -> str:
    return json.dumps(
        [None if first is None else str(first), None if second is None else str(second)]
    )


def ranks_for(*xids: int) -> dict[int, int]:
    return {xid: rank for rank, xid in enumerate(xids)}


def make_report() -> dict[str, Any]:
    """A passing report: every ordering, snapshot and delete check holds."""
    changes = [
        change(pk=1, offset=0, op="r", lsn=5, txid=None, snapshot="first", after_ts=1),
        change(pk=1, offset=1, op="u", lsn=10, txid=1, after_ts=2, sequence=sequence_text(10, 10)),
        change(pk=1, offset=2, op="u", lsn=20, txid=2, after_ts=3, sequence=sequence_text(20, 20)),
        change(pk=1, offset=3, op="u", lsn=30, txid=3, after_ts=4, sequence=sequence_text(30, 30)),
        change(pk=1, offset=4, op="d", lsn=40, txid=3, before_ts=4, sequence=sequence_text(30, 40)),
    ]
    ranks = ranks_for(1, 2, 3)
    return {
        "order": cc.order_checks(changes, ranks),
        "snapshot": cc.snapshot_check(changes, {"customers": 1}),
        "deletes": cc.delete_pairs(changes),
    }


# --- parsing ---------------------------------------------------------------------------------


def test_parse_ts_us_reads_utc_microseconds() -> None:
    assert cc.parse_ts_us("2026-01-01T00:00:00.000123Z") == EPOCH_2026


def test_parse_ts_us_offset_form_gives_the_same_value() -> None:
    assert cc.parse_ts_us("2026-01-01T00:00:00.000123+00:00") == EPOCH_2026


def test_parse_ts_us_converts_other_offsets_to_utc() -> None:
    assert cc.parse_ts_us("2026-01-01T07:00:00.000123+07:00") == EPOCH_2026


def test_parse_ts_us_none_is_none() -> None:
    assert cc.parse_ts_us(None) is None


def test_parse_ts_us_rejects_a_non_date() -> None:
    # The "x" keeps the text from being a digits-only ISO basic date: 8 random hex digits were all
    # decimal in about 2 runs in 100, and fromisoformat then fails with "month must be in 1..12".
    with pytest.raises(ValueError, match="isoformat"):
        cc.parse_ts_us("x" + secrets.token_hex(4))


def test_parse_ts_us_is_numeric_not_textual() -> None:
    # As text "…:00.9Z" sorts after "…:00.10Z"; as microseconds 900 000 is below 1 000 000.
    low = cc.parse_ts_us("2026-01-01T00:00:00.9Z")
    high = cc.parse_ts_us("2026-01-01T00:00:01Z")
    assert low is not None
    assert high is not None
    assert low < high


def test_parse_sequence_two_numbers() -> None:
    assert cc.parse_sequence('["26781904","26781904"]') == (26781904, 26781904)


def test_parse_sequence_null_first() -> None:
    assert cc.parse_sequence('[null,"26780632"]') == (None, 26780632)


def test_parse_sequence_none_is_two_nulls() -> None:
    assert cc.parse_sequence(None) == (None, None)


def test_parse_sequence_rejects_another_shape() -> None:
    with pytest.raises(ValueError, match="two-element"):
        cc.parse_sequence('["1"]')


# --- commit ranks ----------------------------------------------------------------------------


def test_commit_ranks_follow_commit_order() -> None:
    lines = [
        "BEGIN 769",
        "table public.customers: UPDATE: customer_id[bigint]:1",
        COMMIT_LINE.format(xid=769),
        "BEGIN 770",
        COMMIT_LINE.format(xid=770),
    ]
    assert cc.commit_ranks(lines) == {769: 0, 770: 1}


def test_commit_ranks_rank_by_commit_not_begin() -> None:
    lines = ["BEGIN 5", "BEGIN 6", COMMIT_LINE.format(xid=6), COMMIT_LINE.format(xid=5)]
    assert cc.commit_ranks(lines) == {6: 0, 5: 1}


def test_commit_ranks_ignore_an_unfinished_transaction() -> None:
    assert cc.commit_ranks(["BEGIN 9"]) == {}


# --- order checks ----------------------------------------------------------------------------


def streamed(lsns: list[int], **kwargs: Any) -> list[dict[str, Any]]:
    return [change(offset=i, lsn=lsn, txid=i + 1, **kwargs) for i, lsn in enumerate(lsns)]


def test_order_lsn_violation_counted() -> None:
    result = cc.order_checks(streamed([10, 20, 15]), ranks_for(1, 2, 3))
    assert result["lsn_violations"] == 1
    assert result["streamed"] == 3
    assert result["keys_checked"] == 1


def test_order_equal_lsn_is_a_violation() -> None:
    assert cc.order_checks(streamed([10, 10]), ranks_for(1, 2))["lsn_violations"] == 1


def test_order_rank_violation_counted() -> None:
    changes = streamed([10, 20, 30])
    result = cc.order_checks(changes, {1: 0, 2: 2, 3: 1})
    assert result["rank_violations"] == 1
    assert result["lsn_violations"] == 0


def test_order_same_transaction_shares_a_rank() -> None:
    changes = [change(offset=0, lsn=10, txid=7), change(offset=1, lsn=11, txid=7)]
    result = cc.order_checks(changes, {7: 0})
    assert result["rank_violations"] == 0
    assert result["unranked"] == 0


def test_order_unranked_change_counted() -> None:
    result = cc.order_checks(streamed([10, 20]), {1: 0})
    assert result["unranked"] == 1


def test_order_missing_lsn_counted() -> None:
    changes = [change(offset=0, lsn=None), change(offset=1, lsn=5, txid=2)]
    result = cc.order_checks(changes, ranks_for(1, 2))
    assert result["missing_lsn"] == 1
    assert result["lsn_violations"] == 0


def test_order_follows_kafka_offsets_not_list_order() -> None:
    changes = [
        change(offset=2, lsn=30, txid=3),
        change(offset=0, lsn=10, txid=1),
        change(offset=1, lsn=20, txid=2),
    ]
    result = cc.order_checks(changes, ranks_for(1, 2, 3))
    assert result["lsn_violations"] == 0
    assert result["rank_violations"] == 0


def test_order_keys_are_independent() -> None:
    changes = [
        change(pk=1, offset=0, lsn=50, txid=1),
        change(pk=2, offset=1, lsn=10, txid=2),
        change(pk=1, offset=2, lsn=60, txid=3),
    ]
    result = cc.order_checks(changes, ranks_for(1, 2, 3))
    assert result["lsn_violations"] == 0
    assert result["keys_checked"] == 2


def test_order_snapshot_rows_are_not_streamed() -> None:
    changes = [change(op="r", lsn=None, txid=None, snapshot="true", after_ts=1)]
    result = cc.order_checks(changes, {})
    assert result["streamed"] == 0
    assert result["missing_lsn"] == 0
    assert result["unranked"] == 0


def test_updated_at_tie_counted() -> None:
    result = cc.order_checks(streamed([10, 20, 30], after_ts=0), ranks_for(1, 2, 3))
    assert result["updated_at"]["ties"] == 2


def test_updated_at_one_tie_two_comparisons() -> None:
    changes = [
        change(offset=i, lsn=10 + i, txid=i + 1, after_ts=ts) for i, ts in enumerate([1, 1, 2])
    ]
    result = cc.order_checks(changes, ranks_for(1, 2, 3))["updated_at"]
    assert (result["comparisons"], result["ties"], result["inversions"]) == (2, 1, 0)


def test_updated_at_inversion_counted() -> None:
    changes = [change(offset=i, lsn=10 + i, txid=i + 1, after_ts=ts) for i, ts in enumerate([3, 2])]
    result = cc.order_checks(changes, ranks_for(1, 2))["updated_at"]
    assert (result["comparisons"], result["ties"], result["inversions"]) == (1, 0, 1)


def test_updated_at_delete_row_adds_no_comparison() -> None:
    changes = [
        change(offset=0, op="u", lsn=10, txid=1, after_ts=5),
        change(offset=1, op="d", lsn=20, txid=1, before_ts=5),
    ]
    result = cc.order_checks(changes, ranks_for(1))["updated_at"]
    assert result["comparisons"] == 0
    assert result["ties"] == 0


def test_updated_at_includes_snapshot_rows_first() -> None:
    changes = [
        change(offset=0, op="r", lsn=None, txid=None, snapshot="true", after_ts=5),
        change(offset=1, op="u", lsn=10, txid=1, after_ts=5),
    ]
    result = cc.order_checks(changes, ranks_for(1))["updated_at"]
    assert (result["comparisons"], result["ties"]) == (1, 1)


def test_single_change_key_adds_no_comparison() -> None:
    result = cc.order_checks([change(after_ts=9, sequence=sequence_text(1, 2))], {1: 0})
    assert result["updated_at"]["comparisons"] == 0
    assert result["sequence"]["comparisons"] == 0


def test_empty_input_reports_zero_never_skips() -> None:
    result = cc.order_checks([], {})
    assert result["streamed"] == 0
    assert result["keys_checked"] == 0
    assert result["updated_at"] == {"comparisons": 0, "ties": 0, "inversions": 0}
    assert result["sequence"] == {"comparisons": 0, "ties": 0, "inversions": 0, "nulls": 0}


def test_sequence_compares_as_numbers() -> None:
    # As text "9" > "10"; as numbers 9 < 10, so this is strictly rising.
    changes = [
        change(offset=0, lsn=9, txid=1, sequence=sequence_text(9, 9)),
        change(offset=1, lsn=10, txid=2, sequence=sequence_text(10, 10)),
    ]
    result = cc.order_checks(changes, ranks_for(1, 2))["sequence"]
    assert (result["comparisons"], result["ties"], result["inversions"], result["nulls"]) == (
        1,
        0,
        0,
        0,
    )


def test_sequence_null_counted_and_compared_as_minus_one() -> None:
    changes = [
        change(offset=0, lsn=9, txid=1, sequence=sequence_text(None, 9)),
        change(offset=1, lsn=10, txid=2, sequence=sequence_text(None, 10)),
    ]
    result = cc.order_checks(changes, ranks_for(1, 2))["sequence"]
    assert result["nulls"] == 2
    assert result["ties"] == 0
    assert result["inversions"] == 0


def test_sequence_inversion_and_tie_counted() -> None:
    changes = [
        change(offset=0, lsn=1, txid=1, sequence=sequence_text(5, 5)),
        change(offset=1, lsn=2, txid=2, sequence=sequence_text(5, 5)),
        change(offset=2, lsn=3, txid=3, sequence=sequence_text(4, 9)),
    ]
    result = cc.order_checks(changes, ranks_for(1, 2, 3))["sequence"]
    assert (result["ties"], result["inversions"]) == (1, 1)


def test_order_is_idempotent_on_the_same_input() -> None:
    changes = streamed([10, 30, 20], after_ts=1)
    ranks = ranks_for(1, 2, 3)
    assert cc.order_checks(changes, ranks) == cc.order_checks(copy.deepcopy(changes), dict(ranks))


# --- snapshot rows ---------------------------------------------------------------------------


def test_snapshot_ok_one_row_per_seeded_key() -> None:
    changes = [
        change(pk=1, offset=0, op="r", lsn=None, txid=None, snapshot="first"),
        change(pk=2, offset=1, op="r", lsn=None, txid=None, snapshot="true"),
        change(pk=3, offset=2, op="r", lsn=None, txid=None, snapshot="last"),
        change(pk=1, offset=3, op="u", lsn=10),
    ]
    result = cc.snapshot_check(changes, {"customers": 3})
    assert result["ok"] is True
    assert result["flags"] == {"first": 1, "true": 1, "last": 1}
    assert (result["r_rows"], result["keys_with_one_r"], result["keys_with_many_r"]) == (3, 3, 0)


def test_snapshot_two_rows_for_one_key_fail() -> None:
    changes = [
        change(pk=1, offset=0, op="r", snapshot="first"),
        change(pk=1, offset=1, op="r", snapshot="true"),
    ]
    result = cc.snapshot_check(changes, {"customers": 1})
    assert result["keys_with_many_r"] == 1
    assert result["ok"] is False


def test_snapshot_row_after_a_streamed_change_fails() -> None:
    changes = [
        change(pk=1, offset=0, op="u", lsn=10),
        change(pk=1, offset=1, op="r", lsn=None, txid=None, snapshot="true"),
    ]
    result = cc.snapshot_check(changes, {"customers": 1})
    assert result["r_after_streamed"] == 1
    assert result["ok"] is False


def test_snapshot_fewer_rows_than_seeded_keys_fail() -> None:
    changes = [change(pk=1, offset=0, op="r", lsn=None, txid=None, snapshot="first")]
    assert cc.snapshot_check(changes, {"customers": 2})["ok"] is False


def test_snapshot_unknown_flag_fails() -> None:
    changes = [change(pk=1, offset=0, op="r", lsn=None, txid=None, snapshot="false")]
    assert cc.snapshot_check(changes, {"customers": 1})["ok"] is False


# --- delete pairs ----------------------------------------------------------------------------


def test_delete_pairs_with_the_update_of_its_transaction() -> None:
    changes = [
        change(offset=0, op="u", lsn=10, txid=4, after_ts=7),
        change(offset=1, op="d", lsn=11, txid=4, before_ts=7),
    ]
    assert cc.delete_pairs(changes) == {
        "d_rows": 1,
        "paired": 1,
        "unpaired": 0,
        "time_mismatch": 0,
        "ok": True,
    }


def test_delete_without_an_update_is_unpaired() -> None:
    result = cc.delete_pairs([change(offset=0, op="d", lsn=11, txid=4, before_ts=7)])
    assert result["unpaired"] == 1
    assert result["ok"] is False


def test_delete_update_in_another_transaction_is_unpaired() -> None:
    changes = [
        change(offset=0, op="u", lsn=10, txid=3, after_ts=7),
        change(offset=1, op="d", lsn=11, txid=4, before_ts=7),
    ]
    assert cc.delete_pairs(changes)["unpaired"] == 1


def test_delete_time_mismatch_counted() -> None:
    changes = [
        change(offset=0, op="u", lsn=10, txid=4, after_ts=7),
        change(offset=1, op="d", lsn=11, txid=4, before_ts=6),
    ]
    result = cc.delete_pairs(changes)
    assert result["paired"] == 1
    assert result["time_mismatch"] == 1
    assert result["ok"] is False


def test_delete_pairs_with_no_deletes_report_zero() -> None:
    result = cc.delete_pairs([change(offset=0, op="u", lsn=10)])
    assert result["d_rows"] == 0
    assert result["ok"] is True


# --- late products ---------------------------------------------------------------------------


def test_late_products_found_when_the_item_commits_first() -> None:
    items = [change(table="order_items", op="c", lsn=10, product_id=900)]
    products = [change(pk=900, table="products", op="c", lsn=20)]
    assert cc.late_products(items, products, [900]) == {
        "expected": 1,
        "found": 1,
        "missing_product": 0,
    }


def test_late_product_not_late_when_the_product_commits_first() -> None:
    items = [change(table="order_items", op="c", lsn=30, product_id=900)]
    products = [change(pk=900, table="products", op="c", lsn=20)]
    assert cc.late_products(items, products, [900])["found"] == 0


def test_late_product_without_a_product_row_is_missing() -> None:
    items = [change(table="order_items", op="c", lsn=10, product_id=900)]
    assert cc.late_products(items, [], [900]) == {"expected": 1, "found": 0, "missing_product": 1}


# --- row conversion and the prohibitions -----------------------------------------------------


def bronze_row(table: str, after: Mapping[str, Any] | None, **source: Any) -> dict[str, Any]:
    return {
        "before": None,
        "after": dict(after) if after is not None else None,
        "source": {"lsn": 7, "txId": 3, "sequence": None, "snapshot": "false", **source},
        "op": "c",
        "_kafka_metadata_topic": f"shopstream.public.{table}",
        "_kafka_metadata_partition": 2,
        "_kafka_metadata_offset": 9,
        "_kafka_metadata_timestamp": 0,
    }


def test_change_from_row_reads_the_fields() -> None:
    row = bronze_row(
        "customers",
        {
            "customer_id": 4,
            "email": "e",
            "full_name": "n",
            "updated_at": "2026-01-01T00:00:00.000123Z",
        },
    )
    made = cc.change_from_row("customers", row)
    assert (made["pk"], made["partition"], made["offset"], made["op"]) == ((4,), 2, 9, "c")
    assert (made["lsn"], made["txid"], made["snapshot"]) == (7, 3, "false")
    assert made["after_ts"] == EPOCH_2026
    assert made["before_ts"] is None


def test_change_from_row_flags_the_canary_without_carrying_it() -> None:
    token = secrets.token_urlsafe(24)
    email = f"{secrets.token_hex(6)}@example.test"
    row = bronze_row(
        "customers",
        {
            "customer_id": 1,
            "email": email,
            "full_name": token,
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    made = cc.change_from_row("customers", row, canary=token)
    assert made["canary"] is True
    text = json.dumps(made)
    assert token not in text
    assert email not in text


def test_change_from_row_without_the_canary_leaves_the_flag_false() -> None:
    row = bronze_row(
        "customers",
        {
            "customer_id": 1,
            "email": "e",
            "full_name": secrets.token_urlsafe(24),
            "updated_at": "2026-01-01T00:00:00Z",
        },
    )
    assert cc.change_from_row("customers", row, canary=secrets.token_urlsafe(24))["canary"] is False


def test_change_from_row_hashes_a_review_body_and_drops_the_text() -> None:
    body = f"body {secrets.token_hex(8)}"
    row = bronze_row(
        "reviews",
        {"review_id": 1, "product_id": 2, "body": body, "updated_at": "2026-01-01T00:00:00Z"},
    )
    made = cc.change_from_row("reviews", row)
    assert made["body_sha256"] == hashlib.sha256(body.encode()).hexdigest()
    assert body not in json.dumps(made)


def test_change_from_row_reads_the_product_of_an_order_item_and_the_tier() -> None:
    items = bronze_row(
        "order_items",
        {"order_id": 1, "line_no": 2, "product_id": 900, "updated_at": "2026-01-01T00:00:00Z"},
    )
    assert cc.change_from_row("order_items", items)["product_id"] == 900
    tiered = bronze_row(
        "customers",
        {"customer_id": 1, "full_name": "n", "updated_at": "2026-01-01T00:00:00Z", "tier": "gold"},
    )
    assert cc.change_from_row("customers", tiered)["tier_set"] is True


def test_knob_counts_report_counts_and_never_the_canary() -> None:
    token = secrets.token_urlsafe(24)
    expected = hashlib.sha256(token.encode()).hexdigest()
    changes = [
        change(table="customers", op="c", canary=True),
        change(pk=7, table="reviews", op="c", body_sha256=expected),
        change(table="orders", op="u"),
    ]
    workload = {
        "late_products": {"count": 0, "product_ids": []},
        "review": {"review_id": 7, "body_sha256": expected},
    }
    counts = cc.knob_counts(changes, workload)
    assert counts["canary"] == {"rows": 1}
    assert counts["review"] == {"rows": 1, "body_sha256_matches": True}
    assert counts["ops"]["customers"] == {"c": 1}
    assert token not in json.dumps(counts)


def test_knob_counts_review_with_another_hash_does_not_match() -> None:
    changes = [change(pk=7, table="reviews", op="c", body_sha256="0" * 64)]
    workload = {
        "late_products": {"product_ids": []},
        "review": {"review_id": 7, "body_sha256": "1" * 64},
    }
    assert cc.knob_counts(changes, workload)["review"]["body_sha256_matches"] is False


# --- verdict ---------------------------------------------------------------------------------


def test_verdict_go_when_every_check_holds() -> None:
    result = cc.item11_verdict(make_report())
    assert result["verdict"] == "go"
    assert result["fallback"] is None


def test_verdict_go_even_with_updated_at_inversions() -> None:
    report = make_report()
    report["order"]["updated_at"]["inversions"] = 12
    report["order"]["updated_at"]["ties"] = 3
    assert cc.item11_verdict(report)["verdict"] == "go"


def lsn_failed(report: dict[str, Any]) -> dict[str, Any]:
    report["order"]["lsn_violations"] = 2
    return report


def test_verdict_fallback_when_lsn_fails_and_sequence_is_clean() -> None:
    report = lsn_failed(make_report())
    report["order"]["sequence"] = {"comparisons": 9, "ties": 0, "inversions": 0, "nulls": 0}
    result = cc.item11_verdict(report)
    assert result["verdict"] == "fallback"
    assert "source.sequence" in str(result["fallback"])


def test_verdict_fallback_when_only_the_commit_rank_fails() -> None:
    report = make_report()
    report["order"]["rank_violations"] = 1
    report["order"]["sequence"] = {"comparisons": 9, "ties": 0, "inversions": 0, "nulls": 0}
    assert cc.item11_verdict(report)["verdict"] == "fallback"


@pytest.mark.parametrize(
    "sequence",
    [
        {"comparisons": 9, "ties": 1, "inversions": 0, "nulls": 0},
        {"comparisons": 9, "ties": 0, "inversions": 1, "nulls": 0},
        {"comparisons": 9, "ties": 0, "inversions": 0, "nulls": 2},
    ],
)
def test_verdict_inconclusive_when_lsn_fails_and_sequence_is_not_clean(
    sequence: dict[str, int],
) -> None:
    report = lsn_failed(make_report())
    report["order"]["sequence"] = sequence
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_inconclusive_with_a_missing_lsn_and_unclean_sequence() -> None:
    report = make_report()
    report["order"]["missing_lsn"] = 1
    report["order"]["sequence"]["nulls"] = 1
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_inconclusive_with_an_unranked_change() -> None:
    report = make_report()
    report["order"]["unranked"] = 1
    result = cc.item11_verdict(report)
    assert result["verdict"] == "inconclusive"
    assert result["reasons"]


def test_verdict_inconclusive_with_an_unpaired_delete() -> None:
    report = make_report()
    report["deletes"]["unpaired"] = 1
    report["deletes"]["ok"] = False
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_inconclusive_with_a_failed_snapshot_check() -> None:
    report = make_report()
    report["snapshot"]["ok"] = False
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_inconclusive_with_no_streamed_change() -> None:
    report = make_report()
    report["order"]["streamed"] = 0
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_inconclusive_when_bronze_had_not_caught_up() -> None:
    report = make_report()
    report["caught_up"] = False
    assert cc.item11_verdict(report)["verdict"] == "inconclusive"


def test_verdict_does_not_change_its_input() -> None:
    report = make_report()
    before = copy.deepcopy(report)
    cc.item11_verdict(report)
    assert report == before


# --- stdin -----------------------------------------------------------------------------------


def test_read_workload_takes_the_seed_and_the_run() -> None:
    lines = [
        "noise",
        json.dumps({"mode": "seed", "seeded_keys": {"customers": 3}}),
        json.dumps({"mode": "item11", "end_lsn": "0/10"}),
    ]
    seed, run = cc.read_workload(lines)
    assert seed["seeded_keys"] == {"customers": 3}
    assert run["end_lsn"] == "0/10"


def test_read_workload_without_a_run_is_an_error() -> None:
    with pytest.raises(ValueError, match="no workload report"):
        cc.read_workload([json.dumps({"mode": "seed", "seeded_keys": {}})])
