"""Item 2's Spark writer and item 14's Python 3.13 proof. Run by the `spark-job` one-shot.

The one-shot bind-mounts this file into the spike image (Python 3.13, JRE 21, PySpark 4.1.3 and
the Iceberg 1.11.0 jars, all baked at build time). It writes Iceberg v3 tables in the `spike`
warehouse through Lakekeeper and prints one JSON line, the last line of stdout, with the
versions, the driver's and a Python UDF's Python version, and per table the snapshot id, row
count, row digest and deletion-vector evidence. Plan 02-05's readers consume that line.

This is a spike script (ADR-001 Evidence rules), so it has no unit tests. It never reads or
prints a credential: Lakekeeper vends the storage credentials to the JVM through the REST
catalog, and the container holds no key. Nothing resolves from the network at run time.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import row_hash

CATALOG = "rest"
WAREHOUSE = "spike"
NAMESPACE = "spike_v3"
# The four tables in the fixed order every consumer relies on, with the payload column's type.
TABLES: tuple[tuple[str, str], ...] = (
    ("a_variant", "variant"),
    ("b_json_dv", "json_string"),
    ("c_variant_dv", "variant"),
    ("d_json", "json_string"),
)
COLUMNS = ("id", "name", "amount", "ts", "payload")
JSON_COLUMNS = frozenset({"payload"})
DEFAULT_LAKEKEEPER_URL = "http://lakekeeper:8181"
ALLOWED_ORIGINS = frozenset({DEFAULT_LAKEKEEPER_URL})
TIMEOUT_S = 30
TABLE_PROPERTIES = (
    "'format-version'='3',"
    "'write.merge.mode'='merge-on-read',"
    "'write.delete.mode'='merge-on-read',"
    "'write.update.mode'='merge-on-read'"
)

# The four rows every table ends with. Payload numbers stay integers (engines print fractional
# variant numbers differently); the fractional value lives in the DECIMAL column.
FINAL_ROWS: tuple[Mapping[str, str], ...] = (
    {
        "id": "1",
        "name": "a",
        "amount": "1.50",
        "ts": "2026-01-01 00:00:00",
        "payload": '{"k":1,"n":null,"t":["x","y"]}',
    },
    {
        "id": "2",
        "name": "bb",
        "amount": "22.00",
        "ts": "2026-01-02 12:30:00.123456",
        "payload": '{"k":22,"ok":true}',
    },
    {
        "id": "3",
        "name": "c",
        "amount": "-0.25",
        "ts": "2026-01-03 00:00:00",
        "payload": "[1,2,3]",
    },
    {
        "id": "5",
        "name": "é",
        "amount": "5.00",
        "ts": "2026-01-05 23:59:59.999999",
        "payload": '{"k":5,"s":"日本"}',
    },
)

# b_json_dv and c_variant_dv reach FINAL_ROWS the long way: ids 1 to 4, then a MERGE that updates
# id 2 and inserts id 5, then a DELETE of id 4. Both steps run under merge-on-read, which commits
# Puffin deletion vectors. a_variant and d_json get FINAL_ROWS in one INSERT and have none.
INITIAL_ROWS: tuple[Mapping[str, str], ...] = (
    FINAL_ROWS[0],
    {
        "id": "2",
        "name": "b",
        "amount": "2.00",
        "ts": "2026-01-02 00:00:00",
        "payload": '{"k":2}',
    },
    FINAL_ROWS[2],
    {
        "id": "4",
        "name": "d",
        "amount": "4.00",
        "ts": "2026-01-04 00:00:00",
        "payload": '{"k":4}',
    },
)
MERGE_SOURCE: tuple[Mapping[str, str], ...] = (FINAL_ROWS[1], FINAL_ROWS[3])
DELETED_ID = 4
WITH_DELETES = frozenset({"b_json_dv", "c_variant_dv"})


def check_url(url: str) -> None:
    """Refuse any URL that is not plain http to an allowed in-network origin."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "http":
        raise ValueError("only http URLs are allowed")
    origin = f"{parts.scheme}://{parts.netloc}"
    if origin not in ALLOWED_ORIGINS:
        raise ValueError(f"origin {origin} is not allowed")


def http_request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    """Send one request and return (status, body); an HTTP error status is returned, not raised."""
    check_url(url)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            payload: bytes = response.read()
            return int(response.status), payload
    except urllib.error.HTTPError as exc:
        error_payload: bytes = exc.read()
        return int(exc.code), error_payload


def lakekeeper_url() -> str:
    url = os.environ.get("LAKEKEEPER_URL", DEFAULT_LAKEKEEPER_URL).rstrip("/")
    check_url(url)
    return url


def spark_session() -> Any:
    """A local[2] Spark session on the Lakekeeper REST catalog with vended credentials.

    No package coordinates are set, so nothing is resolved at run time: the Iceberg jars sit in
    PySpark's jar directory from the image build.
    """
    from pyspark.sql import SparkSession

    settings = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        f"spark.sql.catalog.{CATALOG}": "org.apache.iceberg.spark.SparkCatalog",
        f"spark.sql.catalog.{CATALOG}.type": "rest",
        f"spark.sql.catalog.{CATALOG}.uri": f"{lakekeeper_url()}/catalog",
        f"spark.sql.catalog.{CATALOG}.warehouse": WAREHOUSE,
        f"spark.sql.catalog.{CATALOG}.header.X-Iceberg-Access-Delegation": "vended-credentials",
        "spark.sql.session.timeZone": "UTC",
        "spark.sql.warehouse.dir": "/tmp/spark-warehouse",
        "spark.local.dir": "/tmp/spark-local",
        "spark.ui.enabled": "false",
        "spark.driver.memory": "1g",
    }
    builder = SparkSession.builder.master("local[2]").appName("shopstream-spark-v3-job")
    for key, value in settings.items():
        builder = builder.config(key, value)
    spark = builder.getOrCreate()
    spark.sparkContext.setLogLevel("WARN")
    return spark


def sql_text(value: str) -> str:
    """A SQL string literal; the values here are the script's own constants."""
    return "'" + value.replace("\\", "\\\\").replace("'", "''") + "'"


def select_rows(rows: tuple[Mapping[str, str], ...], payload_type: str) -> str:
    """A SELECT of literal rows in COLUMNS order; VARIANT payloads go through parse_json."""
    selects = []
    for row in rows:
        payload = sql_text(row["payload"])
        if payload_type == "variant":
            payload = f"parse_json({payload})"
        selects.append(
            f"SELECT CAST({row['id']} AS BIGINT) AS id, {sql_text(row['name'])} AS name, "
            f"CAST({row['amount']} AS DECIMAL(10,2)) AS amount, "
            f"CAST({sql_text(row['ts'])} AS TIMESTAMP) AS ts, {payload} AS payload"
        )
    return " UNION ALL ".join(selects)


def catalog_prefix() -> str:
    """The warehouse's REST path prefix, read from Lakekeeper's catalog config."""
    base = lakekeeper_url()
    status, body = http_request("GET", f"{base}/catalog/v1/config?warehouse={WAREHOUSE}", {}, None)
    if status != 200:
        raise RuntimeError(f"catalog config returned HTTP {status}")
    return str(json.loads(body)["defaults"]["prefix"])


def drop_table(name: str) -> None:
    """Drop one table and have Lakekeeper purge its files; a missing table is fine.

    `DROP TABLE ... PURGE` in Spark also reads the dropped table's manifests on the client, and
    Lakekeeper's own purge worker can delete them first, which failed one run in five. The
    REST drop with purgeRequested leaves the purge to Lakekeeper alone, so a rerun is stable.
    """
    url = (
        f"{lakekeeper_url()}/catalog/v1/{catalog_prefix()}/namespaces/{NAMESPACE}"
        f"/tables/{name}?purgeRequested=true"
    )
    status, _ = http_request("DELETE", url, {}, None)
    if status not in {204, 404}:
        raise RuntimeError(f"dropping {name} returned HTTP {status}")


def create_table(spark: Any, name: str, payload_type: str) -> str:
    """Drop and recreate one table as v3 with merge-on-read, so every run starts clean."""
    full = f"{CATALOG}.{NAMESPACE}.{name}"
    column = "VARIANT" if payload_type == "variant" else "STRING"
    drop_table(name)
    spark.sql(
        f"CREATE TABLE {full} (id BIGINT, name STRING, amount DECIMAL(10,2), "
        f"ts TIMESTAMP, payload {column}) USING iceberg TBLPROPERTIES ({TABLE_PROPERTIES})"
    )
    return full


def write_tables(spark: Any) -> list[str]:
    """Create the tables and write their rows; returns the names written, in TABLES order."""
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.{NAMESPACE}")
    written = []
    for name, payload_type in TABLES:
        full = create_table(spark, name, payload_type)
        if name in WITH_DELETES:
            spark.sql(f"INSERT INTO {full} {select_rows(INITIAL_ROWS, payload_type)}")
            spark.sql(
                f"MERGE INTO {full} t USING ({select_rows(MERGE_SOURCE, payload_type)}) s "
                f"ON t.id = s.id WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
            )
            spark.sql(f"DELETE FROM {full} WHERE id = {DELETED_ID}")
        else:
            spark.sql(f"INSERT INTO {full} {select_rows(FINAL_ROWS, payload_type)}")
        written.append(name)
    return written


def format_version(name: str) -> int:
    """The table's format-version as Lakekeeper's loadTable response reports it."""
    url = f"{lakekeeper_url()}/catalog/v1/{catalog_prefix()}/namespaces/{NAMESPACE}/tables/{name}"
    status, body = http_request("GET", url, {}, None)
    if status != 200:
        raise RuntimeError(f"loadTable returned HTTP {status} for {name}")
    return int(json.loads(body)["metadata"]["format-version"])


def table_report(spark: Any, name: str, payload_type: str) -> dict[str, Any]:
    """Snapshot, row digest and deletion-vector evidence for one table's current snapshot."""
    full = f"{CATALOG}.{NAMESPACE}.{name}"
    current = spark.sql(f"SELECT snapshot_id FROM {full}.refs WHERE name = 'main'").collect()
    snapshot_id = int(current[0]["snapshot_id"])
    snapshots = [
        {
            "snapshot_id": int(row["snapshot_id"]),
            "operation": str(row["operation"]),
            "added_dvs": int(row["added_dvs"] or 0),
        }
        for row in spark.sql(
            f"SELECT snapshot_id, operation, summary['added-dvs'] AS added_dvs "
            f"FROM {full}.snapshots ORDER BY committed_at"
        ).collect()
    ]
    delete_files = [
        {
            "content": int(row["content"]),
            "file_format": str(row["file_format"]),
            "record_count": int(row["record_count"]),
        }
        for row in spark.sql(
            f"SELECT content, file_format, record_count FROM {full}.all_files "
            f"WHERE content <> 0 ORDER BY content, file_format, record_count"
        ).collect()
    ]
    # VARIANT is cast to JSON text in SQL: its raw Arrow form is not canonical across engines.
    payload = "to_json(payload)" if payload_type == "variant" else "payload"
    arrow = spark.sql(
        f"SELECT id, name, amount, ts, {payload} AS payload FROM {full} VERSION AS OF {snapshot_id}"
    ).toArrow()
    row_count, digest = row_hash.arrow_table_digest(arrow, COLUMNS, JSON_COLUMNS)
    return {
        "table": name,
        "payload_type": payload_type,
        "format_version": format_version(name),
        "snapshot_id": snapshot_id,
        "row_count": row_count,
        "table_digest": digest,
        "snapshots": snapshots,
        "delete_files": delete_files,
    }


def version_report(spark: Any) -> dict[str, str]:
    """Spark, Iceberg, PySpark and JVM versions, and the Python version of the driver and a UDF."""
    import pyspark
    from pyspark.sql.functions import udf

    jvm = spark.sparkContext._jvm
    driver = ".".join(str(part) for part in sys.version_info[:3])

    def worker_python() -> str:
        import sys

        return ".".join(str(part) for part in sys.version_info[:3])

    worker_version = udf(worker_python, "string")
    found = [row["v"] for row in spark.range(1).select(worker_version().alias("v")).collect()]
    if len(found) != 1 or not found[0]:
        raise RuntimeError(f"the Python UDF returned {len(found)} values, expected exactly one")
    return {
        "spark_version": str(spark.version),
        "iceberg_version": str(jvm.org.apache.iceberg.IcebergBuild.version()),
        "pyspark_version": str(pyspark.__version__),
        "java_version": str(jvm.java.lang.System.getProperty("java.runtime.version")),
        "java_vendor": str(jvm.java.lang.System.getProperty("java.vendor")),
        "python_driver": driver,
        "python_udf": str(found[0]),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, help="also write the JSON line to this file")
    args = parser.parse_args(argv)
    spark = spark_session()
    try:
        report: dict[str, Any] = dict(version_report(spark))
        written = write_tables(spark)
        report["namespace"] = NAMESPACE
        report["tables"] = [
            table_report(spark, name, payload_type)
            for name, payload_type in TABLES
            if name in written
        ]
    finally:
        spark.stop()
    line = json.dumps(report, ensure_ascii=False)
    if args.out is not None:
        args.out.write_text(line + "\n", encoding="utf-8")
    print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
