"""Unit tests for scripts/read_v3.py: the item 2 verdict rule and the pure helpers.

No DuckDB, PyIceberg or Polars is imported (CI installs no spike group): the job and the matrix
are hand-built dicts shaped like spark_v3_job.py's JSON line and read_v3.py's matrix rows.
"""

from __future__ import annotations

import copy
import secrets
from collections.abc import Mapping
from typing import Any

import pytest
import read_v3 as rv

TABLES = (
    ("a_variant", "variant"),
    ("b_json_dv", "json_string"),
    ("c_variant_dv", "variant"),
    ("d_json", "json_string"),
)
WITH_DVS = {"b_json_dv", "c_variant_dv"}
READ = ("duckdb", "pyiceberg", "polars")
DIGEST = "ab" * 32
ADR_FALLBACKS = {"JSON string column", "v2 with position deletes"}


def make_job() -> dict[str, Any]:
    """A job in the shape Spark prints: format 3 everywhere, deletion vectors on b and c only."""
    tables = []
    for index, (name, payload_type) in enumerate(TABLES):
        has_dv = name in WITH_DVS
        snapshots = [{"snapshot_id": 1, "operation": "append", "added_dvs": 0}]
        if has_dv:
            snapshots.append({"snapshot_id": 2, "operation": "overwrite", "added_dvs": 1})
        tables.append(
            {
                "table": name,
                "payload_type": payload_type,
                "format_version": 3,
                "snapshot_id": 1000 + index,
                "row_count": 4,
                "table_digest": DIGEST,
                "snapshots": snapshots,
                "delete_files": (
                    [{"content": 1, "file_format": "PUFFIN", "record_count": 1}] if has_dv else []
                ),
            }
        )
    return {"namespace": "spike_v3", "tables": tables}


def make_matrix(
    job: Mapping[str, Any], failing: Mapping[tuple[str, str], str] | None = None
) -> list[dict[str, Any]]:
    """The 16 cells: Spark's reference and every reader matching, except the `failing` cells."""
    failing = failing or {}
    cells = rv.reference_rows(job)
    for table in job["tables"]:
        for reader in READ:
            result = failing.get((reader, table["table"]))
            if result is None:
                cells.append(rv.loaded(table, reader, 4, DIGEST, table["snapshot_id"]))
            elif result == "cannot-load":
                cells.append(rv.cannot_load(table, reader, "ValidationError: Unsupported field"))
            else:
                cells.append(rv.loaded(table, reader, 4, "cd" * 32, table["snapshot_id"]))
    return cells


def fail(reader: str, *tables: str, result: str = "cannot-load") -> dict[tuple[str, str], str]:
    return {(reader, table): result for table in tables}


def fallback_pairs(verdict: Mapping[str, Any]) -> list[tuple[str, str]]:
    return [(f["reader"], f["fallback"]) for f in verdict["fallbacks"]]


def test_every_reader_matching_with_deletion_vectors_is_go() -> None:
    job = make_job()
    verdict = rv.item2_verdict(job, make_matrix(job))
    assert verdict["verdict"] == "go"
    assert verdict["fallbacks"] == []
    assert verdict["reasons"]


def test_pyiceberg_and_polars_failing_variant_tables_take_the_json_string_fallback() -> None:
    job = make_job()
    failing = {
        **fail("pyiceberg", "a_variant", "c_variant_dv"),
        **fail("polars", "a_variant", "c_variant_dv"),
    }
    verdict = rv.item2_verdict(job, make_matrix(job, failing))
    assert verdict["verdict"] == "fallback"
    assert fallback_pairs(verdict) == [
        ("pyiceberg", "JSON string column"),
        ("polars", "JSON string column"),
    ]


def test_a_mismatch_on_both_variant_tables_is_attributed_to_variant_too() -> None:
    job = make_job()
    failing = fail("duckdb", "a_variant", "c_variant_dv", result="mismatch")
    verdict = rv.item2_verdict(job, make_matrix(job, failing))
    assert verdict["verdict"] == "fallback"
    assert fallback_pairs(verdict) == [("duckdb", "JSON string column")]


def test_a_reader_that_fails_the_deletion_vector_tables_takes_v2() -> None:
    job = make_job()
    failing = fail("duckdb", "b_json_dv", "c_variant_dv", result="mismatch")
    verdict = rv.item2_verdict(job, make_matrix(job, failing))
    assert verdict["verdict"] == "fallback"
    assert fallback_pairs(verdict) == [("duckdb", "v2 with position deletes")]


def test_a_reader_failing_variant_and_deletion_vectors_takes_both_fallbacks() -> None:
    job = make_job()
    failing = fail("polars", "a_variant", "b_json_dv", "c_variant_dv")
    verdict = rv.item2_verdict(job, make_matrix(job, failing))
    assert verdict["verdict"] == "fallback"
    assert fallback_pairs(verdict) == [
        ("polars", "JSON string column"),
        ("polars", "v2 with position deletes"),
    ]


def drop_dv_snapshots(job: dict[str, Any]) -> None:
    for snapshot in job["tables"][1]["snapshots"]:
        snapshot["added_dvs"] = 0


def drop_delete_files(job: dict[str, Any]) -> None:
    job["tables"][2]["delete_files"] = []


def parquet_delete_files(job: dict[str, Any]) -> None:
    job["tables"][1]["delete_files"] = [{"content": 1, "file_format": "PARQUET", "record_count": 1}]


def format_version_two(job: dict[str, Any]) -> None:
    job["tables"][0]["format_version"] = 2


@pytest.mark.parametrize(
    "break_job",
    [drop_dv_snapshots, drop_delete_files, parquet_delete_files, format_version_two],
)
def test_spark_without_deletion_vector_evidence_or_format_3_takes_v2(
    break_job: Any,
) -> None:
    job = make_job()
    break_job(job)
    verdict = rv.item2_verdict(job, make_matrix(job))
    assert verdict["verdict"] == "fallback"
    assert fallback_pairs(verdict) == [("spark", "v2 with position deletes")]


@pytest.mark.parametrize("reader", READ)
def test_a_reader_failing_the_plain_table_is_inconclusive_and_named(reader: str) -> None:
    job = make_job()
    verdict = rv.item2_verdict(job, make_matrix(job, fail(reader, "d_json", result="mismatch")))
    assert verdict["verdict"] == "inconclusive"
    assert verdict["fallbacks"] == []
    assert any(reader in reason and "d_json" in reason for reason in verdict["reasons"])


@pytest.mark.parametrize(
    "failing_tables",
    [
        ("c_variant_dv",),
        ("a_variant", "b_json_dv"),
        ("a_variant",),
        ("b_json_dv",),
    ],
    ids=["only-c", "a-and-b-but-c-matches", "only-a", "only-b"],
)
def test_a_pattern_neither_variant_nor_deletion_vectors_explain_is_inconclusive(
    failing_tables: tuple[str, ...],
) -> None:
    job = make_job()
    verdict = rv.item2_verdict(job, make_matrix(job, fail("pyiceberg", *failing_tables)))
    assert verdict["verdict"] == "inconclusive"
    assert verdict["fallbacks"] == []
    assert any("pyiceberg" in reason for reason in verdict["reasons"])


def test_spark_digests_that_differ_across_tables_are_inconclusive() -> None:
    job = make_job()
    job["tables"][3]["table_digest"] = "ef" * 32
    verdict = rv.item2_verdict(job, make_matrix(job))
    assert verdict["verdict"] == "inconclusive"
    assert any("digest" in reason for reason in verdict["reasons"])


@pytest.mark.parametrize("index", [0, 3], ids=["a_variant", "d_json"])
def test_delete_files_on_a_table_without_deletes_are_inconclusive(index: int) -> None:
    job = make_job()
    job["tables"][index]["delete_files"] = [
        {"content": 1, "file_format": "PUFFIN", "record_count": 1}
    ]
    verdict = rv.item2_verdict(job, make_matrix(job))
    assert verdict["verdict"] == "inconclusive"
    assert any(TABLES[index][0] in reason for reason in verdict["reasons"])


def test_a_job_missing_a_table_is_inconclusive() -> None:
    job = make_job()
    matrix = make_matrix(job)
    del job["tables"][1]
    verdict = rv.item2_verdict(job, matrix)
    assert verdict["verdict"] == "inconclusive"
    assert any("b_json_dv" in reason for reason in verdict["reasons"])


@pytest.mark.parametrize(
    ("reader", "table"),
    [("polars", "d_json"), ("spark", "a_variant"), ("duckdb", "c_variant_dv")],
)
def test_a_matrix_missing_a_cell_is_inconclusive_naming_the_cell(reader: str, table: str) -> None:
    job = make_job()
    matrix = [c for c in make_matrix(job) if (c["reader"], c["table"]) != (reader, table)]
    assert len(matrix) == 15
    verdict = rv.item2_verdict(job, matrix)
    assert verdict["verdict"] == "inconclusive"
    assert any(reader in reason and table in reason for reason in verdict["reasons"])


def test_inconclusive_wins_over_a_fallback_and_names_no_fallback() -> None:
    job = make_job()
    drop_dv_snapshots(job)
    matrix = make_matrix(job, fail("polars", "d_json"))
    verdict = rv.item2_verdict(job, matrix)
    assert verdict["verdict"] == "inconclusive"
    assert verdict["fallbacks"] == []


def test_fallbacks_only_ever_use_adr_001s_two_wordings() -> None:
    job = make_job()
    failing = {
        **fail("duckdb", "b_json_dv", "c_variant_dv"),
        **fail("pyiceberg", "a_variant", "c_variant_dv"),
        **fail("polars", "a_variant", "b_json_dv", "c_variant_dv"),
    }
    verdict = rv.item2_verdict(job, make_matrix(job, failing))
    assert verdict["verdict"] == "fallback"
    assert {fallback for _, fallback in fallback_pairs(verdict)} <= ADR_FALLBACKS


@pytest.mark.parametrize(
    ("count", "digest", "expected"),
    [
        (4, DIGEST, "match"),
        (4, "cd" * 32, "mismatch"),
        (3, DIGEST, "mismatch"),
        (0, DIGEST, "mismatch"),
    ],
    ids=["equal", "same-count-other-digest", "other-count", "empty"],
)
def test_cell_result_needs_count_and_digest_to_equal_sparks(
    count: int, digest: str, expected: str
) -> None:
    spark_table = {"row_count": 4, "table_digest": DIGEST}
    assert rv.cell_result(spark_table, count, digest) == expected


def test_error_text_keeps_the_type_and_the_first_line_only() -> None:
    exc = ValueError("Unsupported field type: 'variant'\nsecond line\nthird line")
    assert rv.error_text(exc) == "ValueError: Unsupported field type: 'variant'"


def test_error_text_is_at_most_200_characters() -> None:
    assert len(rv.error_text(RuntimeError("x" * 500))) == 200


def test_an_unloadable_table_is_cannot_load_with_empty_counts_never_zero_rows() -> None:
    table = make_job()["tables"][0]
    cell = rv.cannot_load(table, "pyiceberg", "ValidationError: Unsupported field type")
    assert cell["result"] == "cannot-load"
    assert cell["row_count"] is None
    assert cell["table_digest"] is None
    assert cell["snapshot_id"] == table["snapshot_id"]
    assert cell["error"] == "ValidationError: Unsupported field type"


def test_the_matrix_prints_tables_and_readers_in_one_fixed_order() -> None:
    job = make_job()
    cells = make_matrix(job)
    shuffled = list(reversed(cells))
    ordered = rv.build_matrix(job, [c for c in shuffled if c["reader"] != "spark"])
    assert [(c["table"], c["reader"]) for c in ordered] == [
        (name, reader) for name, _ in TABLES for reader in rv.READERS
    ]


def test_reference_rows_are_sparks_own_cells() -> None:
    job = make_job()
    rows = rv.reference_rows(job)
    assert [r["table"] for r in rows] == [name for name, _ in TABLES]
    assert {r["reader"] for r in rows} == {"spark"}
    assert {r["result"] for r in rows} == {"reference"}
    assert all(r["table_digest"] == DIGEST and r["row_count"] == 4 for r in rows)


def test_polars_storage_options_map_the_s3_properties() -> None:
    key_id, key_material, token = (secrets.token_hex(8) for _ in range(3))
    properties = {
        "s3.access-key-id": key_id,
        "s3.secret-access-key": key_material,
        "s3.session-token": token,
        "s3.endpoint": "http://seaweedfs:8333/",
        "s3.region": "us-east-1",
        "s3.path-style-access": "true",
    }
    assert rv.polars_storage_options(properties) == {
        "aws_access_key_id": key_id,
        "aws_secret_access_key": key_material,
        "aws_session_token": token,
        "aws_endpoint_url": "http://seaweedfs:8333",
        "aws_region": "us-east-1",
        "aws_allow_http": "true",
    }


def test_polars_storage_options_strip_only_one_trailing_slash() -> None:
    base = {
        "s3.access-key-id": "i",
        "s3.secret-access-key": "m",
        "s3.session-token": "t",
        "s3.region": "us-east-1",
    }
    with_slash = rv.polars_storage_options({**base, "s3.endpoint": "http://h:1/"})
    without_slash = rv.polars_storage_options({**base, "s3.endpoint": "http://h:1"})
    assert with_slash["aws_endpoint_url"] == without_slash["aws_endpoint_url"] == "http://h:1"


@pytest.mark.parametrize("name", ["a_variant", "d_json", "_t1"])
def test_identifier_accepts_plain_names(name: str) -> None:
    assert rv.identifier(name) == name


@pytest.mark.parametrize("name", ["", "a b", "t; DROP TABLE x", "1abc", "a.b", "a'b"])
def test_identifier_refuses_anything_else(name: str) -> None:
    with pytest.raises(ValueError, match="plain SQL identifier"):
        rv.identifier(name)


def test_the_hand_built_job_is_not_mutated_by_the_verdict() -> None:
    job = make_job()
    matrix = make_matrix(job, fail("pyiceberg", "a_variant", "c_variant_dv"))
    before = (copy.deepcopy(job), copy.deepcopy(matrix))
    rv.item2_verdict(job, matrix)
    assert (job, matrix) == before


def test_scrub_values_removes_every_credential_value_from_an_error_text() -> None:
    key_id, token = secrets.token_hex(8), secrets.token_hex(8)
    text = f"PanicException: signing with {key_id} and {token} failed, again {key_id}"
    scrubbed = rv.scrub_values(text, [key_id, "", token])
    assert key_id not in scrubbed
    assert token not in scrubbed
    assert (
        scrubbed
        == "PanicException: signing with <redacted> and <redacted> failed, again <redacted>"
    )


def test_error_text_scrubs_a_secret_that_straddles_the_cut_before_truncating() -> None:
    secret = secrets.token_hex(20)
    exc = RuntimeError("x" * 190 + secret)
    text = rv.error_text(exc, [secret])
    assert len(text) <= 200
    assert secret[:6] not in text


def test_error_text_marks_a_scrubbed_secret_that_fits_before_the_cut() -> None:
    secret = secrets.token_hex(20)
    text = rv.error_text(RuntimeError("x" * 150 + secret), [secret])
    assert text.endswith("<redacted>")
    assert secret[:6] not in text


def test_error_text_without_secrets_keeps_the_type_and_first_line_at_200_characters() -> None:
    exc = ValueError("first line\nsecond line")
    assert rv.error_text(exc, []) == "ValueError: first line"
    assert len(rv.error_text(RuntimeError("y" * 500), ())) == 200


def test_scrub_values_leaves_short_values_alone_and_replaces_long_ones() -> None:
    value = secrets.token_hex(8)
    assert len(value) == 16
    text = f"it is true that {value} leaked"
    assert rv.scrub_values(text, [value, "true", ""]) == "it is true that <redacted> leaked"
