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
