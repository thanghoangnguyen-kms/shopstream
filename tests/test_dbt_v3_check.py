"""Unit tests for scripts/dbt_v3_check.py: the item 3 verdict rule and its pure helpers.

No DuckDB, boto3 or network is imported (CI installs no spike group): each stage is a hand-built
dict shaped like `inspect`'s JSON line. The live functions (`metadata_file`, `inspect`) need a
running stack and are exercised by the evidence run, not here.
"""

from __future__ import annotations

import copy
import gzip
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import dbt_v3_check as chk
import pytest

UUID = "01a0fc18-37c4-7131-ad1c-1b7ca9efba02"
FIRST_SNAPSHOT = 7144986360310025750


def snapshots(*operations: str, first_id: int = FIRST_SNAPSHOT) -> list[dict[str, Any]]:
    return [
        {
            "sequence_number": index + 1,
            "snapshot_id": first_id + index,
            "operation": operation,
            "summary": {},
        }
        for index, operation in enumerate(operations)
    ]


def make_stage(
    stage: int,
    *,
    file_version: int | None = 3,
    catalog_version: int | None = 3,
    table_uuid: str = UUID,
    ops: tuple[str, ...] | None = None,
    first_id: int = FIRST_SNAPSHOT,
    rows: list[list[Any]] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    """An `inspect` result; the defaults are what a correct run of that stage prints."""
    if ops is None:
        ops = ("append",) if stage == 1 else ("append", "delete", "overwrite")
    if rows is None:
        rows = copy.deepcopy(chk.EXPECTED_BATCH_1 if stage == 1 else chk.EXPECTED_AFTER_MERGE)
    return {
        "table": "silver_spike.inc_v3",
        "catalog_format_version": catalog_version,
        "table_uuid": table_uuid,
        "file_format_version": file_version,
        "file_table_uuid": table_uuid,
        "file_snapshots": snapshots(*ops, first_id=first_id),
        "rows": rows,
        "error": error,
    }


def good_stages() -> dict[int, dict[str, Any] | None]:
    return {1: make_stage(1), 2: make_stage(2), 3: make_stage(3)}


def reasons_text(verdict: Mapping[str, Any]) -> str:
    return " | ".join(verdict["reasons"])


def test_three_correct_stages_are_go() -> None:
    verdict = chk.item3_verdict(good_stages())
    assert verdict["verdict"] == "go"
    assert verdict["reasons"]


def test_a_stage_whose_metadata_file_is_format_version_2_is_fallback() -> None:
    stages = good_stages()
    stages[1] = make_stage(1, file_version=2, catalog_version=2)
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    text = reasons_text(verdict)
    assert "stage 1" in text
    assert "metadata.json" in text


def test_a_different_table_uuid_in_stage_2_means_the_run_recreated_the_table() -> None:
    stages = good_stages()
    stages[2] = make_stage(2, table_uuid="deadbeef-0000-0000-0000-000000000000")
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    assert "stage 2" in reasons_text(verdict)


def test_a_first_snapshot_other_than_stage_1s_append_is_fallback() -> None:
    stages = good_stages()
    stages[2] = make_stage(2, first_id=FIRST_SNAPSHOT + 1000)
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    assert "first snapshot" in reasons_text(verdict)


@pytest.mark.parametrize(
    "rows",
    [
        [[1, "a", False], [2, "b", False]],
        [[1, "a", False], [2, "b-updated", False], [2, "b-updated", False]],
        [[1, "a", False], [2, "b-updated", False], [3, "c", False]],
        [[2, "b-updated", False]],
    ],
    ids=["update-missing", "duplicate-id-2", "surviving-id-3", "id-1-gone"],
)
def test_stage_2_rows_other_than_the_merged_pair_are_fallback(rows: list[list[Any]]) -> None:
    stages = good_stages()
    stages[2] = make_stage(2, rows=rows)
    stages[3] = make_stage(3, rows=rows)
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    assert "stage 2" in reasons_text(verdict)


@pytest.mark.parametrize(
    "ops",
    [
        ("append", "overwrite"),
        ("append", "overwrite", "delete"),
        ("append", "delete", "overwrite", "overwrite"),
        ("append", "append"),
    ],
)
def test_stage_2_operations_other_than_delete_then_overwrite_are_fallback(
    ops: tuple[str, ...],
) -> None:
    stages = good_stages()
    stages[2] = make_stage(2, ops=ops)
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    assert str(list(ops)) in reasons_text(verdict)


def test_file_and_catalog_format_versions_that_disagree_are_inconclusive() -> None:
    stages = good_stages()
    stages[1] = make_stage(1, file_version=3, catalog_version=2)
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "inconclusive"
    assert "stage 1" in reasons_text(verdict)


@pytest.mark.parametrize("stage", [1, 2, 3])
def test_a_missing_stage_is_inconclusive_and_named(stage: int) -> None:
    stages = good_stages()
    stages[stage] = None
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "inconclusive"
    assert f"stage {stage}" in reasons_text(verdict)


def test_a_stage_that_carries_an_error_is_inconclusive_and_the_error_is_kept() -> None:
    stages = good_stages()
    failed = make_stage(2, error="Error: Deletion vector file is not a valid Puffin file")
    failed["rows"] = None
    stages[2] = failed
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "inconclusive"
    text = reasons_text(verdict)
    assert "stage 2" in text
    assert "Puffin" in text


def test_stage_3_rows_that_differ_from_stage_2s_are_inconclusive() -> None:
    stages = good_stages()
    stages[3] = make_stage(3, rows=[[1, "a", False], [2, "b-updated", False], [4, "d", False]])
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "inconclusive"
    assert "stage 3" in reasons_text(verdict)


def test_stage_3_with_a_different_table_uuid_is_fallback() -> None:
    stages = good_stages()
    stages[3] = make_stage(3, table_uuid="deadbeef-0000-0000-0000-000000000000")
    verdict = chk.item3_verdict(stages)
    assert verdict["verdict"] == "fallback"
    assert "stage 3" in reasons_text(verdict)


def test_an_inconclusive_finding_wins_over_a_fallback_finding() -> None:
    stages = good_stages()
    stages[1] = make_stage(1, file_version=2, catalog_version=2)
    stages[3] = None
    assert chk.item3_verdict(stages)["verdict"] == "inconclusive"


def test_the_rule_never_mutates_its_inputs() -> None:
    stages = good_stages()
    stages[2] = make_stage(2, ops=("append", "overwrite"))
    before = copy.deepcopy(stages)
    chk.item3_verdict(stages)
    assert stages == before


def test_stage_findings_for_a_correct_stage_is_empty() -> None:
    first = make_stage(1)
    assert chk.stage_findings(1, first, None) == []
    assert chk.stage_findings(2, make_stage(2), first) == []


def test_split_location_returns_the_bucket_and_key() -> None:
    assert chk.split_location("s3://warehouse/abc/metadata/00000-x.gz.metadata.json") == (
        "warehouse",
        "abc/metadata/00000-x.gz.metadata.json",
    )
    with pytest.raises(ValueError, match="bucket and key"):
        chk.split_location("file:///tmp/x")


def test_gunzip_if_gzipped_reads_both_a_gzip_and_a_plain_metadata_file() -> None:
    document = json.dumps({"format-version": 3}).encode()
    assert chk.gunzip_if_gzipped(gzip.compress(document)) == document
    assert chk.gunzip_if_gzipped(document) == document


def write_stage(path: Path, stage: Mapping[str, Any]) -> str:
    path.write_text("noise line\n" + json.dumps(stage) + "\n\n", encoding="utf-8")
    return str(path)


def test_the_verdict_subcommand_exits_0_for_go_and_1_otherwise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [write_stage(tmp_path / f"s{n}.txt", make_stage(n)) for n in (1, 2, 3)]
    assert chk.main(["verdict", *paths]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "go"

    paths[1] = write_stage(tmp_path / "s2.txt", make_stage(2, ops=("append", "overwrite")))
    assert chk.main(["verdict", *paths]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "fallback"


def test_the_verdict_subcommand_treats_an_unreadable_file_as_a_missing_stage(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [write_stage(tmp_path / f"s{n}.txt", make_stage(n)) for n in (1, 2)]
    paths.append(str(tmp_path / "absent.txt"))
    assert chk.main(["verdict", *paths]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "inconclusive"


@pytest.mark.parametrize(
    "argv",
    [[], ["verdict"], ["verdict", "a", "b"], ["reset", "extra"], ["unknown"]],
)
def test_a_usage_error_exits_2(argv: list[str]) -> None:
    assert chk.main(argv) == 2


# --- the second-engine leg: Spark, PyIceberg and the Puffin check -----------------------------

PUFFIN_DELETE_SIZES = (338, 336)


def puffin_blob(size: int) -> bytes:
    """A deletion-vector file as dbt-duckdb on DuckDB 1.5.5 writes it: PFA1 at both ends."""
    return b"PFA1" + b"x" * (size - 8) + b"PFA1"


def make_engines(**overrides: Any) -> dict[str, Any]:
    """An `engines` result; the defaults are what a correct run on the fallback prints."""
    engines: dict[str, Any] = {
        "table": "silver_spike.inc_v3",
        "spark_version": "4.1.3",
        "iceberg_version": "1.11.0",
        "spark_rows": copy.deepcopy(chk.EXPECTED_AFTER_MERGE),
        "pyiceberg_version": "0.12.0",
        "pyiceberg_rows": copy.deepcopy(chk.EXPECTED_AFTER_MERGE),
        "delete_files": [
            {
                "file_format": "PUFFIN",
                "content": 1,
                "size": size,
                "head_magic": True,
                "tail_magic": True,
            }
            for size in PUFFIN_DELETE_SIZES
        ],
        "error": None,
    }
    engines.update(overrides)
    return engines


def test_puffin_magic_is_true_at_both_ends_of_a_puffin_file() -> None:
    assert chk.PUFFIN_MAGIC == b"PFA1"
    assert chk.puffin_magic(puffin_blob(338)) == (True, True)


def test_puffin_magic_is_false_for_a_raw_deletion_vector_blob() -> None:
    assert chk.puffin_magic(bytes(range(42))) == (False, False)


@pytest.mark.parametrize("size", [0, 1, 4, 7])
def test_puffin_magic_is_false_for_anything_shorter_than_8_bytes(size: int) -> None:
    assert chk.puffin_magic(b"PFA1PFA1"[:size]) == (False, False)


def test_puffin_magic_reports_each_end_on_its_own() -> None:
    assert chk.puffin_magic(b"PFA1" + b"x" * 30) == (True, False)
    assert chk.puffin_magic(b"x" * 30 + b"PFA1") == (False, True)


def test_engine_findings_is_empty_for_a_correct_engines_line() -> None:
    assert chk.engine_findings(make_engines()) == []


def test_engine_findings_on_a_missing_engines_line_is_one_inconclusive_finding() -> None:
    ((kind, message),) = chk.engine_findings(None)
    assert kind == "inconclusive"
    assert "second-engine" in message


def test_an_engines_line_that_carries_an_error_is_inconclusive_and_keeps_the_error() -> None:
    ((kind, message),) = chk.engine_findings(make_engines(error="RuntimeError: loadTable 503"))
    assert kind == "inconclusive"
    assert "loadTable 503" in message


def test_spark_rows_other_than_the_merged_pair_are_a_fallback_finding_naming_spark() -> None:
    findings = chk.engine_findings(make_engines(spark_rows=[[1, "a", False], [2, "b", False]]))
    assert [kind for kind, _ in findings] == ["fallback"]
    assert "Spark" in findings[0][1]
    assert "PyIceberg" not in findings[0][1]


def test_pyiceberg_rows_other_than_the_merged_pair_are_a_fallback_finding_naming_it() -> None:
    findings = chk.engine_findings(make_engines(pyiceberg_rows=[[2, "b-updated", False]]))
    assert [kind for kind, _ in findings] == ["fallback"]
    assert "PyIceberg" in findings[0][1]
    assert "Spark" not in findings[0][1]


def test_an_empty_delete_file_list_is_inconclusive_because_there_is_nothing_to_check() -> None:
    ((kind, message),) = chk.engine_findings(make_engines(delete_files=[]))
    assert kind == "inconclusive"
    assert "deletion vector" in message


def test_a_delete_file_that_is_not_puffin_is_a_fallback_finding() -> None:
    raw = {"file_format": "PARQUET", "content": 1, "size": 42, "head_magic": False}
    raw["tail_magic"] = False
    findings = chk.engine_findings(make_engines(delete_files=[raw]))
    assert [kind for kind, _ in findings] == ["fallback"]
    assert "Puffin" in findings[0][1]


@pytest.mark.parametrize("missing", ["head_magic", "tail_magic"])
def test_a_puffin_file_without_the_magic_at_one_end_is_a_fallback_finding(missing: str) -> None:
    engines = make_engines()
    engines["delete_files"][1][missing] = False
    findings = chk.engine_findings(engines)
    assert [kind for kind, _ in findings] == ["fallback"]
    assert "valid Puffin file" in findings[0][1]


def test_the_verdict_without_an_engines_argument_is_what_it_was_before() -> None:
    verdict = chk.item3_verdict(good_stages())
    assert verdict["verdict"] == "go"
    assert chk.ENGINES_GO_REASON not in verdict["reasons"]
    assert len(verdict["reasons"]) == 1


def test_good_stages_and_a_good_engines_line_are_go_with_both_reasons() -> None:
    stages_only = chk.item3_verdict(good_stages())
    verdict = chk.item3_verdict(good_stages(), engines=make_engines())
    assert verdict["verdict"] == "go"
    assert verdict["reasons"] == [*stages_only["reasons"], chk.ENGINES_GO_REASON]
    assert "PFA1" in chk.ENGINES_GO_REASON


def test_a_missing_engines_line_makes_the_four_input_verdict_inconclusive() -> None:
    verdict = chk.item3_verdict(good_stages(), engines=None)
    assert verdict["verdict"] == "inconclusive"
    assert "second-engine" in reasons_text(verdict)


def test_a_bad_engines_line_makes_good_stages_fallback() -> None:
    engines = make_engines(spark_rows=[[1, "a", False]])
    verdict = chk.item3_verdict(good_stages(), engines=engines)
    assert verdict["verdict"] == "fallback"
    assert "Spark" in reasons_text(verdict)


def test_an_inconclusive_engines_finding_wins_over_a_fallback_stage_finding() -> None:
    stages = good_stages()
    stages[1] = make_stage(1, file_version=2, catalog_version=2)
    verdict = chk.item3_verdict(stages, engines=make_engines(error="OSError: no route"))
    assert verdict["verdict"] == "inconclusive"
    assert "no route" in reasons_text(verdict)


def test_an_inconclusive_stage_finding_wins_over_a_fallback_engines_finding() -> None:
    stages = good_stages()
    stages[3] = None
    verdict = chk.item3_verdict(stages, engines=make_engines(pyiceberg_rows=[]))
    assert verdict["verdict"] == "inconclusive"
    assert "stage 3" in reasons_text(verdict)


def test_the_four_input_rule_never_mutates_its_inputs() -> None:
    stages = good_stages()
    engines = make_engines(spark_rows=[[1, "a", False]])
    stages_before, engines_before = copy.deepcopy(stages), copy.deepcopy(engines)
    chk.item3_verdict(stages, engines=engines)
    assert stages == stages_before
    assert engines == engines_before


def write_engines(path: Path, engines: Mapping[str, Any]) -> str:
    return write_stage(path, engines)


def stage_paths(tmp_path: Path) -> list[str]:
    return [write_stage(tmp_path / f"s{n}.txt", make_stage(n)) for n in (1, 2, 3)]


def test_the_verdict_subcommand_reads_a_fourth_path_as_the_engines_line(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [*stage_paths(tmp_path), write_engines(tmp_path / "e.txt", make_engines())]
    assert chk.main(["verdict", *paths]) == 0
    verdict = json.loads(capsys.readouterr().out.splitlines()[-1])
    assert verdict["verdict"] == "go"
    assert verdict["reasons"][-1] == chk.ENGINES_GO_REASON


def test_the_verdict_subcommand_exits_1_for_a_fourth_file_that_fails_the_rule(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = make_engines(pyiceberg_rows=[[1, "a", False]])
    paths = [*stage_paths(tmp_path), write_engines(tmp_path / "e.txt", bad)]
    assert chk.main(["verdict", *paths]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "fallback"


def test_the_verdict_subcommand_treats_an_unreadable_fourth_file_as_inconclusive(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    paths = [*stage_paths(tmp_path), str(tmp_path / "absent.txt")]
    assert chk.main(["verdict", *paths]) == 1
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["verdict"] == "inconclusive"


@pytest.mark.parametrize("count", [0, 1, 2, 5])
def test_the_verdict_subcommand_takes_three_or_four_paths_and_nothing_else(count: int) -> None:
    assert chk.main(["verdict", *[f"p{n}" for n in range(count)]]) == 2


def test_the_engines_subcommand_takes_no_argument() -> None:
    assert chk.main(["engines", "extra"]) == 2


def test_the_engines_subcommand_prints_the_line_and_exits_1_when_it_carries_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(chk, "engines", lambda: make_engines())
    assert chk.main(["engines"]) == 0
    assert json.loads(capsys.readouterr().out.splitlines()[-1])["error"] is None

    monkeypatch.setattr(chk, "engines", lambda: make_engines(error="RuntimeError: no catalog"))
    assert chk.main(["engines"]) == 1
    assert "no catalog" in capsys.readouterr().out
