"""Item 7's live runner: switch gold by REST rename under a concurrent DuckDB poller.

Run inside the `spark-job` one-shot: `python /app/gold_switch_probe.py`. The last line of stdout is
one JSON line. Precondition: gold holds publish_id A on every object, gold_candidate holds B and
gold_retired is empty; the probe refuses to start otherwise and ends in the same state, so a rerun
starts from the same place. A broken state is reset with `gold_reset.py` and the two dbt builds,
never by building over existing objects (an incremental rebuild appends).

A publisher renames each gold object through Lakekeeper's REST rename in three steps (gold to
gold_retired, gold_candidate to gold, gold_retired to gold_candidate) while a poller thread, on its
own DuckDB connection attached with `MAX_TABLE_STALENESS '0s'`, reads every object's publish_id
through `gold_switch.poll` on a fixed-rate schedule. The poller counts what it saw before the
retry (raw), what the rule accepted and what it refused (`PublishInProgress`). A natural run
may see no mixed read by luck, so a control run with a gap between objects widens the window and
shows the poller can see it.

This is a spike script (ADR-001 Evidence rules): the live parts need a running stack and have no
unit tests; the rule, the statistics and the verdict are in `gold_switch.py` and are tested. The
heavy imports (duckdb) sit inside `read_v3.duckdb_connect`, so importing this module in CI is
safe. Every REST call goes through the origin-allowlisted `spark_v3_job.http_request`, and every
name comes from fixed constants through `read_v3.identifier`.
"""

from __future__ import annotations

import argparse
import functools
import json
import sys
import threading
import time
from collections.abc import Sequence
from typing import Any

import gold_reset
import gold_switch
import spark_v3_job
from read_v3 import duckdb_connect, error_text, identifier
from spark_v3_job import WAREHOUSE, http_request, lakekeeper_url

MAX_TABLE_STALENESS = "0s"
PUBLISH_A = "A"
PUBLISH_B = "B"
RESET_HINT = (
    "python /app/gold_reset.py gold gold_candidate gold_retired, then the B build "
    "(--vars '{publish_id: B, gold_schema: gold_candidate}') and the A build "
    "(--vars '{publish_id: A}') with dbt --target lk --select +tag:gold"
)
JSON_HEADERS = {"Content-Type": "application/json"}


@functools.cache
def rest_base() -> str:
    """The warehouse's REST path: `<lakekeeper>/catalog/v1/<prefix>`."""
    return f"{lakekeeper_url()}/catalog/v1/{spark_v3_job.catalog_prefix()}"


def attach_connection() -> Any:
    """A DuckDB connection attached to the warehouse as `lk` with table staleness 0s."""
    con = duckdb_connect()
    con.execute(
        f"ATTACH '{WAREHOUSE}' AS lk (TYPE iceberg, "
        f"ENDPOINT '{lakekeeper_url()}/catalog', AUTHORIZATION_TYPE 'none', "
        f"MAX_TABLE_STALENESS '{MAX_TABLE_STALENESS}')"
    )
    return con


def make_reader(con: Any, namespace: str) -> Any:
    """A `read(object)` for gold_switch: its distinct publish_ids (at most two), or None if absent.

    DuckDB's `does not exist` catalog error is the missing case. Any other error propagates.
    """
    ns = identifier(namespace)

    def read(obj: str) -> list[str] | None:
        try:
            rows = con.execute(
                f"SELECT DISTINCT publish_id FROM lk.{ns}.{identifier(obj)} LIMIT 2"
            ).fetchall()
        except Exception as exc:
            if "does not exist" in str(exc):
                return None
            raise
        return [str(row[0]) for row in rows]

    return read


def rename(source: tuple[str, str], destination: tuple[str, str]) -> None:
    """One REST rename, (namespace, name) to (namespace, name); anything but 204 is an error."""
    body = {
        "source": {"namespace": [identifier(source[0])], "name": identifier(source[1])},
        "destination": {
            "namespace": [identifier(destination[0])],
            "name": identifier(destination[1]),
        },
    }
    status, payload = http_request(
        "POST", f"{rest_base()}/tables/rename", JSON_HEADERS, json.dumps(body).encode()
    )
    if status != 204:
        raise RuntimeError(
            f"rename {source} to {destination} returned HTTP {status}: "
            f"{payload.decode(errors='replace')[:200]}"
        )


def ensure_namespace(namespace: str) -> None:
    """Create a namespace through the REST API; 409 means it already exists."""
    body = json.dumps({"namespace": [identifier(namespace)], "properties": {}}).encode()
    status, payload = http_request("POST", f"{rest_base()}/namespaces", JSON_HEADERS, body)
    if status not in {200, 409}:
        raise RuntimeError(
            f"creating namespace {namespace} returned HTTP {status}: "
            f"{payload.decode(errors='replace')[:200]}"
        )


def check_state(con: Any) -> dict[str, Any]:
    """Where every gold object is: `{"ok": bool, "state": {...}, "problems": [...]}`.

    Expected: every object in gold holds A, every object in gold_candidate holds B, each with a
    publish_id column, and gold_retired holds no table.
    """
    state: dict[str, Any] = {}
    problems: list[str] = []
    for namespace, expected in ((gold_switch.GOLD, PUBLISH_A), (gold_switch.CANDIDATE, PUBLISH_B)):
        read = make_reader(con, namespace)
        cells: dict[str, Any] = {}
        for obj in gold_switch.OBJECTS:
            try:
                ids = read(obj)
                columns: list[str] = []
                if ids is not None:
                    described = con.execute(
                        f"DESCRIBE lk.{identifier(namespace)}.{identifier(obj)}"
                    ).fetchall()
                    columns = [str(row[0]) for row in described]
            except Exception as exc:
                cells[obj] = {"error": error_text(exc)}
                problems.append(f"{namespace}.{obj}: {error_text(exc)}")
                continue
            cells[obj] = {"publish_ids": ids, "has_publish_id_column": "publish_id" in columns}
            if ids != [expected]:
                problems.append(f"{namespace}.{obj} holds {ids}, expected {[expected]}")
            if ids is not None and "publish_id" not in columns:
                problems.append(f"{namespace}.{obj} has no publish_id column")
        state[namespace] = cells
    retired = gold_reset.list_tables(gold_switch.RETIRED)
    state[gold_switch.RETIRED] = retired
    if retired:
        problems.append(f"{gold_switch.RETIRED} holds tables {retired}")
    return {"ok": not problems, "state": state, "problems": problems}


def precheck(con: Any) -> dict[str, Any]:
    """Refuse to start unless gold holds A, gold_candidate holds B and gold_retired is empty.

    Returns the state found. On failure print it and the reset to run, then exit 1.
    """
    ensure_namespace(gold_switch.RETIRED)
    found = check_state(con)
    if found["ok"]:
        return found
    print(json.dumps({"precheck": "failed", **found}), flush=True)
    print(
        f"precheck failed: {'; '.join(found['problems'])}\nReset with: {RESET_HINT}",
        file=sys.stderr,
    )
    sys.exit(1)


VIEW_NAMESPACE = "gold_view_probe"
VIEW_NAME = "v"


def view_check(con: Any) -> dict[str, Any]:
    """Create a view through Lakekeeper's REST API, list it, try to read it in DuckDB, drop it.

    Returns view_created, view_listed, duckdb_reads_view and duckdb_error (None when DuckDB read
    it), plus the REST statuses. The view and its namespace are dropped afterwards, even on error.
    """
    namespace = identifier(VIEW_NAMESPACE)
    name = identifier(VIEW_NAME)
    result: dict[str, Any] = {
        "view_created": False,
        "view_listed": False,
        "duckdb_reads_view": False,
        "duckdb_error": None,
        "create_status": None,
        "list_status": None,
    }
    ensure_namespace(namespace)
    request = {
        "name": name,
        "schema": {
            "type": "struct",
            "schema-id": 0,
            "fields": [{"id": 1, "name": "x", "required": False, "type": "int"}],
        },
        "view-version": {
            "version-id": 1,
            "timestamp-ms": int(time.time() * 1000),
            "schema-id": 0,
            "summary": {"operation": "create"},
            "representations": [{"type": "sql", "sql": "SELECT 1 AS x", "dialect": "duckdb"}],
            "default-namespace": [namespace],
        },
        "properties": {},
    }
    views_url = f"{rest_base()}/namespaces/{namespace}/views"
    try:
        status, _ = http_request("POST", views_url, JSON_HEADERS, json.dumps(request).encode())
        result["create_status"] = status
        result["view_created"] = status == 200
        status, body = http_request("GET", views_url, {}, None)
        result["list_status"] = status
        if status == 200:
            listed = json.loads(body).get("identifiers") or []
            result["view_listed"] = any(
                item.get("name") == name and list(item.get("namespace", [])) == [namespace]
                for item in listed
            )
        try:
            con.execute(f"SELECT * FROM lk.{namespace}.{name}").fetchall()
            result["duckdb_reads_view"] = True
        except Exception as exc:
            result["duckdb_error"] = error_text(exc)
    finally:
        http_request("DELETE", f"{views_url}/{name}", {}, None)
        http_request("DELETE", f"{rest_base()}/namespaces/{namespace}", {}, None)
    return result


def versions(con: Any) -> dict[str, str | None]:
    """DuckDB's version and its iceberg extension's version."""
    import duckdb

    row = con.execute(
        "SELECT extension_version FROM duckdb_extensions() WHERE extension_name = 'iceberg'"
    ).fetchone()
    return {"duckdb": duckdb.__version__, "iceberg_extension": str(row[0]) if row else None}


def run_switches(
    label: str,
    switches: int,
    gap_ms: int,
    period_ms: int,
    retry_ms: int,
    hold_ms: int,
) -> dict[str, Any]:
    """Switch every gold object `switches` times while a poller reads through the publish-id rule.

    The poller starts one hold before the first switch and stops one hold after the last. Between
    objects of one switch the publisher sleeps `gap_ms`. Returns the counts, the directions, the
    accepted ids and the raw timings (`poll_starts_s`, `switch_ms`) for the statistics.
    """
    if switches < 0 or switches % 2:
        raise ValueError("switches must be a non-negative even number")
    reader = make_reader(attach_connection(), gold_switch.GOLD)
    gold_switch.read_once(reader, gold_switch.OBJECTS)  # warm-up: loads metadata, not counted

    counts: dict[str, Any] = {
        "raw_ok": 0,
        "raw_mixed": 0,
        "raw_missing": 0,
        "retried_ok": 0,
        "publish_in_progress": 0,
        "accepted_mixed": 0,
        "accepted_missing": 0,
        "poll_errors": 0,
        "first_poll_error": None,
    }
    sequence: list[str] = []
    starts: list[float] = []
    stop = threading.Event()
    retry_s = retry_ms / 1000

    def poller() -> None:
        next_start = time.monotonic()
        while not stop.is_set():
            wait = next_start - time.monotonic()
            if wait > 0 and stop.wait(wait):
                break
            started = time.monotonic()
            starts.append(started)
            next_start = max(next_start + period_ms / 1000, started)
            try:
                result = gold_switch.poll(reader, gold_switch.OBJECTS, retry_s, time.sleep)
            except gold_switch.PublishInProgress as exc:
                counts[f"raw_{exc.first}"] += 1
                counts["publish_in_progress"] += 1
                continue
            except Exception as exc:
                counts["poll_errors"] += 1
                counts["first_poll_error"] = counts["first_poll_error"] or error_text(exc)
                continue
            counts[f"raw_{result.first_kind}"] += 1
            counts["retried_ok"] += int(result.retried)
            if result.kind != "ok":
                counts[f"accepted_{result.kind}"] += 1
            sequence.append(str(next(iter(result.ids.values()))))

    thread = threading.Thread(target=poller, name=f"poller-{label}")
    thread.start()
    held = PUBLISH_A
    a_to_b = b_to_a = 0
    durations_ms: list[float] = []
    try:
        time.sleep(hold_ms / 1000)
        for _ in range(switches):
            began = time.monotonic()
            for index, obj in enumerate(gold_switch.OBJECTS):
                if index and gap_ms:
                    time.sleep(gap_ms / 1000)
                for source, destination in gold_switch.rename_steps(obj):
                    rename(source, destination)
            durations_ms.append((time.monotonic() - began) * 1000)
            if held == PUBLISH_A:
                a_to_b += 1
                held = PUBLISH_B
            else:
                b_to_a += 1
                held = PUBLISH_A
            time.sleep(hold_ms / 1000)
    finally:
        stop.set()
        thread.join()
    return {
        "label": label,
        "switches": switches,
        "retry_ms": retry_ms,
        "transitions": gold_switch.transitions("".join(sequence)),
        "timing": gold_switch.interval_stats(starts, durations_ms),
        "a_to_b": a_to_b,
        "b_to_a": b_to_a,
        "gap_ms": gap_ms,
        "polls": len(starts),
        **counts,
        "accepted_ids_seen": sorted(set(sequence)),
        "accepted_sequence": "".join(sequence),
        "poll_starts_s": starts,
        "switch_ms": durations_ms,
    }


def public_run(run: dict[str, Any]) -> dict[str, Any]:
    """The run as printed: the raw timing lists are dropped."""
    return {k: v for k, v in run.items() if k not in {"poll_starts_s", "switch_ms"}}


def even(text: str) -> int:
    value = int(text)
    if value < 0 or value % 2:
        raise argparse.ArgumentTypeError("must be a non-negative even number")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Item 7: gold blue/green by REST rename")
    parser.add_argument("--switches", type=even, default=40)
    parser.add_argument("--period-ms", type=int, default=100)
    parser.add_argument("--retry-ms", type=int, default=250)
    parser.add_argument("--hold-ms", type=int, default=1000)
    parser.add_argument("--control-switches", type=even, default=10)
    parser.add_argument("--control-gap-ms", type=int, default=60)
    parser.add_argument("--no-view-check", action="store_true")
    args = parser.parse_args(argv)

    con = attach_connection()
    print(f"max_table_staleness={MAX_TABLE_STALENESS}", file=sys.stderr, flush=True)
    viewed = None if args.no_view_check else view_check(con)
    before = precheck(con)
    columns_ok = all(
        cell.get("has_publish_id_column") is True
        for namespace in (gold_switch.GOLD, gold_switch.CANDIDATE)
        for cell in before["state"][namespace].values()
    )
    runs: dict[str, dict[str, Any]] = {}
    runs["natural"] = run_switches(
        "natural", args.switches, 0, args.period_ms, args.retry_ms, args.hold_ms
    )
    if args.control_switches:
        runs["control"] = run_switches(
            "control",
            args.control_switches,
            args.control_gap_ms,
            args.period_ms,
            args.retry_ms,
            args.hold_ms,
        )
    end = check_state(con)
    report: dict[str, Any] = {
        "versions": versions(con),
        "settings": {
            "period_ms": args.period_ms,
            "retry_ms": args.retry_ms,
            "hold_ms": args.hold_ms,
            "control_gap_ms": args.control_gap_ms,
            "max_table_staleness": MAX_TABLE_STALENESS,
            "objects": list(gold_switch.OBJECTS),
        },
        "runs": {label: public_run(run) for label, run in runs.items()},
        "end_state_ok": end["ok"],
    }
    if not end["ok"]:
        report["end_state"] = end
    exit_code = 0 if end["ok"] else 1
    if viewed is not None:
        report["view_check"] = viewed
        report["verdict"] = gold_switch.item7_verdict(
            viewed,
            runs["natural"],
            runs.get("control"),
            columns_ok=columns_ok,
            end_state_ok=bool(end["ok"]),
        )
        if report["verdict"]["verdict"] == "inconclusive":
            exit_code = 1
    print(json.dumps(report, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
