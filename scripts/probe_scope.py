"""Item 1 credential-scope probe: do vended credentials stay inside their own table's prefix?

Run by the `probe` one-shot in infra/compose.yaml (ADR-001 go criterion 1) inside the Compose
network, because Lakekeeper vends `s3.endpoint = http://seaweedfs:8333/`, which the macOS host
cannot resolve. One run is one probe at a time: it first drops whatever an interrupted run left
(the `probe_scope` namespace, its tables and the root marker key), then proves four things.

  1. Scope. A vended key for table `a` can PUT, GET, list (with the trailing slash), DELETE and
     multipart-upload inside table `a`'s location, and is denied (403 AccessDenied) for the same
     operations on table `b`'s existing keys, on the look-alike prefix `<a>x/` and at the
     warehouse root (GET, PUT, DELETE and both list forms). Every denial has a positive control:
     the same operation on the same existing key with the lakekeeper static key must be 2xx.
  2. Vending. `loadTable` with the delegation header returns `storage-credentials` for the table
     location, without the header it returns none, and the expiry fits the warehouse's 3600 s.
  3. Trust. AssumeRole of LakekeeperVendedRole, by its full ARN, succeeds for lakekeeper (the
     control) and is denied with HTTP 403 for probe-other, admin and a run-time garbage key. A
     400 is never counted as a denial.
  4. Queues (PLAT-11). The maintenance queues Lakekeeper lists in /management/v1/info and its
     OpenAPI task-queue paths. The per-name queue config route answers 200 for any name, so it
     is recorded as non-evidence only.

The result is mapped onto ADR-001's Figure 1 by `verdict`. Output holds statuses and error codes
only: never a key, a session token or a response body. `--json` prints one compact line with a
fixed key order, and `--read-only` expects the own-prefix writes to be denied, so Week 5's
`just test-authz` can rerun it as a reader agent.

Exit codes: 0 for a go verdict, 1 for any other verdict or a failure, 2 for a usage or
environment error.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import secrets
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from typing import Any, Protocol, cast

# Copied from lakekeeper_bootstrap.py so both files stay standalone.
S3_ENDPOINT = "http://seaweedfs:8333"
BUCKET = "warehouse"
WAREHOUSE_NAME = "spike"
REGION = "local-01"
ROLE_ARN = "arn:aws:iam::000000000000:role/LakekeeperVendedRole"
STS_TOKEN_VALIDITY_S = 3600
TIMEOUT_S = 30
ALLOWED_ORIGINS = frozenset({"http://lakekeeper:8181", "http://seaweedfs:8333"})
DEFAULT_LAKEKEEPER_URL = "http://lakekeeper:8181"

EXPIRY_SKEW_S = 60
NAMESPACE = "probe_scope"
TABLE_A = "a"
TABLE_B = "b"
DELEGATION = {"X-Iceberg-Access-Delegation": "vended-credentials"}
MULTIPART_PART_BYTES = 5 * 2**20
MULTIPART_TOTAL_BYTES = 12 * 2**20
ROOT_MARKER = "probe-scope-root-marker"
NOT_A_QUEUE = "definitely_not_a_queue"
ROLE_SESSION_NAME = "probe-scope"
ROLE_TARGET = "LakekeeperVendedRole"
GO_BRANCH = "All engines: vended credentials"
WIDE_BRANCH = "Vended but bucket-wide"
FAIL_BRANCH = "STS fails"

AMBIENT_AWS_VARIABLES = (
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "AWS_PROFILE",
)
REQUIRED_VARIABLES = (
    "LAKEKEEPER_S3_KEY",
    "LAKEKEEPER_S3_SECRET",
    "PROBE_OTHER_KEY",
    "PROBE_OTHER_SECRET",
    "SEAWEEDFS_ADMIN_KEY",
    "SEAWEEDFS_ADMIN_SECRET",
)
VENDED_FIELDS = ("s3.access-key-id", "s3.secret-access-key", "s3.session-token")
SCHEMA = {
    "type": "struct",
    "schema-id": 0,
    "fields": [{"id": 1, "name": "id", "required": False, "type": "long"}],
}

# The five operations on the own table, and what each other target is asked. The order is the
# order the probe runs them in: reads before the DELETE that would remove what they read.
_OWN_OPERATIONS = ("put", "get", "list", "delete", "multipart")
_WRITE_OPERATIONS = frozenset({"put", "delete", "multipart"})
_SIBLING_OPERATIONS = ("get_metadata", "get", "put", "list", "multipart", "delete")
_ROOT_OPERATIONS = ("get", "put", "list_prefix_empty", "list_no_prefix", "delete")
_CONTROL_SIBLING_OPERATIONS = ("get_metadata", "put", "get", "list", "multipart", "delete")
_CONTROL_ROOT_OPERATIONS = ("put", "get", "list_prefix_empty", "list_no_prefix", "delete")


class ProbeSetupError(RuntimeError):
    """A step the probe needs before it can judge anything failed; names the step and status."""


class S3Like(Protocol):
    """The slice of boto3's S3 client the probe uses."""

    def put_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def list_objects_v2(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def list_buckets(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def create_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def upload_part(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def complete_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...
    def abort_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...


class StsLike(Protocol):
    def assume_role(self, **kwargs: Any) -> Mapping[str, Any]: ...


# --- seams: network and clock ---------------------------------------------------------------------


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


def _client_config() -> Any:
    from botocore.config import Config

    return Config(
        region_name=REGION,
        retries={"total_max_attempts": 1},
        connect_timeout=5,
        read_timeout=TIMEOUT_S,
        s3={"addressing_style": "path"},
    )


def make_s3_client(
    access_key_id: str, secret_access_key: str, session_token: str | None = None
) -> S3Like:
    """An S3 client on explicit keys only. boto3 is imported here: CI does not install it."""
    import boto3

    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        aws_session_token=session_token,
        config=_client_config(),
    )
    return cast(S3Like, client)


def make_sts_client(access_key_id: str, secret_access_key: str) -> StsLike:
    import boto3

    client = boto3.client(
        "sts",
        endpoint_url=S3_ENDPOINT,
        region_name=REGION,
        aws_access_key_id=access_key_id,
        aws_secret_access_key=secret_access_key,
        config=_client_config(),
    )
    return cast(StsLike, client)


def now() -> float:
    import time

    return time.time()


# --- calling and classifying ------------------------------------------------------------------------


def _status_of(document: object) -> int:
    meta = document.get("ResponseMetadata") if isinstance(document, Mapping) else None
    status = meta.get("HTTPStatusCode") if isinstance(meta, Mapping) else None
    return status if isinstance(status, int) else 0


def _call(
    client: object, operation: str, **kwargs: Any
) -> tuple[int, str | None, Mapping[str, Any] | None]:
    """One client call as (status, error code, response). Only the error path hides the body."""
    method = getattr(client, operation)
    try:
        response = method(**kwargs)
    except Exception as exc:
        failure = getattr(exc, "response", None)
        if not isinstance(failure, Mapping):
            raise
        error = failure.get("Error")
        code = error.get("Code") if isinstance(error, Mapping) else None
        return _status_of(failure), code if isinstance(code, str) else None, None
    body = response.get("Body") if isinstance(response, Mapping) else None
    close = getattr(body, "close", None)
    if callable(close):
        close()
    return _status_of(response), None, response


def s3_call(client: object, operation: str, **kwargs: Any) -> tuple[int, str | None]:
    """(HTTP status, error code) of one call. An exception without a `response` propagates.

    boto3 raises on 4xx. A garbage key answers 403 with no parseable Error block, so the code is
    None then (research Pitfall 2). Also used for STS calls: both clients share the shape.
    """
    status, code, _ = _call(client, operation, **kwargs)
    return status, code


def classify_s3(status: int, code: str | None) -> str:
    """allow, deny, missing, invalid or error. Only a 403 AccessDenied proves a denial."""
    if 200 <= status < 300:
        return "allow"
    if status == 403:
        return "deny" if code == "AccessDenied" else "error"
    if status == 404:
        return "missing"
    if status == 400:
        return "invalid"
    return "error"


def classify_sts(status: int, code: str | None = None) -> str:
    """A 403 is a denial with or without an Error block; a 400 is never one."""
    if 200 <= status < 300:
        return "allow"
    if status == 403:
        return "deny"
    if status == 400:
        return "invalid"
    return "error"


# --- pure helpers ---------------------------------------------------------------------------------------


def expected_matrix(read_only: bool) -> tuple[tuple[str, str, str, str], ...]:
    """(principal, target, operation, expected) in the one fixed order the probe runs them.

    `vended` is table a's vended key; `lakekeeper` is the static-key positive control, run on the
    same existing keys the vended key is denied on.
    """
    rows: list[tuple[str, str, str, str]] = []
    for operation in _OWN_OPERATIONS:
        denied = read_only and operation in _WRITE_OPERATIONS
        rows.append(("vended", "own", operation, "deny" if denied else "allow"))
    rows.extend(("vended", "sibling", operation, "deny") for operation in _SIBLING_OPERATIONS)
    rows.append(("vended", "lookalike", "put", "deny"))
    rows.extend(("vended", "root", operation, "deny") for operation in _ROOT_OPERATIONS)
    rows.extend(("lakekeeper", "sibling", op, "allow") for op in _CONTROL_SIBLING_OPERATIONS)
    rows.extend(("lakekeeper", "root", op, "allow") for op in _CONTROL_ROOT_OPERATIONS)
    return tuple(rows)


def _strip_one_slash(text: str) -> str:
    return text[:-1] if text.endswith("/") else text


def table_key(location: str) -> str:
    """The object key for an `s3://warehouse/...` location: exact text, one trailing slash off."""
    head = f"s3://{BUCKET}/"
    if not location.startswith(head):
        raise ValueError("unexpected table location")
    return _strip_one_slash(location[len(head) :])


def _first_credentials(load_table_json: Mapping[str, Any]) -> Mapping[str, Any] | None:
    entries = load_table_json.get("storage-credentials")
    if not isinstance(entries, list) or not entries:
        return None
    first = entries[0]
    return first if isinstance(first, Mapping) else None


def vended_config(load_table_json: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """The first storage-credentials entry's config, or None."""
    entry = _first_credentials(load_table_json)
    config = entry.get("config") if entry is not None else None
    return config if isinstance(config, Mapping) else None


def expires_in_s(config: Mapping[str, Any], at: float) -> int | None:
    """Seconds from `at` to the credential's expiry; None when the field is absent or unreadable."""
    raw = config.get("s3.session-token-expires-at-ms")
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    try:
        milliseconds = int(raw)
    except ValueError:
        return None
    return round(milliseconds / 1000 - at)


def _queue_names_in_paths(paths: Iterable[str]) -> set[str]:
    names: set[str] = set()
    for path in paths:
        parts = path.split("/")
        if "task-queue" in parts:
            index = parts.index("task-queue") + 1
            if index < len(parts) and parts[index] and not parts[index].startswith("{"):
                names.add(parts[index])
    return names


def _holds_expire_and_snapshot(name: str) -> bool:
    return "expire" in name and "snapshot" in name


def _holds_orphan(name: str) -> bool:
    return "orphan" in name


def classify_queues(info: Mapping[str, Any], openapi_paths: Iterable[str]) -> dict[str, str]:
    """present, absent or inconclusive for the two maintenance queues PLAT-11 asks about.

    A name counts only when it holds both `expire` and `snapshot`, or `orphan`, so
    tabular_expiration and tabular_purge are neither. An empty or missing `queues` list is
    inconclusive, never absent. The result does not depend on the order of either input.
    """
    listed = info.get("queues")
    names = (
        {name for name in listed if isinstance(name, str)} if isinstance(listed, list) else set()
    )
    if not names:
        return {"expire_snapshots": "inconclusive", "orphan_removal": "inconclusive"}
    lowered = {name.lower() for name in names | _queue_names_in_paths(openapi_paths)}
    expire = any(_holds_expire_and_snapshot(name) for name in lowered)
    orphan = any(_holds_orphan(name) for name in lowered)
    return {
        "expire_snapshots": "present" if expire else "absent",
        "orphan_removal": "present" if orphan else "absent",
    }


def _verdict_inconclusive(reason: str) -> tuple[str, str]:
    return "inconclusive", reason


def _vending_problem(vending: Mapping[str, Any]) -> str | None:
    if vending.get("without_header") is not False:
        return "loadTable without the delegation header returned credentials"
    if vending.get("prefix_matches_location") is not True:
        return "the storage-credentials prefix is not the table location"
    expires = vending.get("expires_in_s")
    if not isinstance(expires, int) or expires <= 0:
        return "the credential expiry is missing or already past"
    if expires > STS_TOKEN_VALIDITY_S + EXPIRY_SKEW_S:
        return "the credential expiry is further away than the warehouse's validity"
    return None


def verdict(result: Mapping[str, Any]) -> tuple[str, str]:
    """Map one probe result onto ADR-001 Figure 1: (verdict, branch or reason).

    go, fallback (with the Figure 1 branch), inconclusive (a control or a status could not be
    trusted, so no branch is named) or trust-breach (another identity can assume the vended role).
    """
    assume = [row for row in result.get("assume_role") or [] if isinstance(row, Mapping)]
    matrix = [row for row in result.get("matrix") or [] if isinstance(row, Mapping)]
    vending = result.get("vending")
    vending = vending if isinstance(vending, Mapping) else {}
    if any(r.get("principal") != "lakekeeper" and r.get("observed") == "allow" for r in assume):
        return "trust-breach", "an identity other than lakekeeper can assume LakekeeperVendedRole"
    control = [r for r in assume if r.get("principal") == "lakekeeper"]
    if not control or control[0].get("observed") != "allow":
        return _verdict_inconclusive("lakekeeper's own AssumeRole is not allowed (the control)")
    if any(r.get("observed") not in {"allow", "deny"} for r in assume):
        return _verdict_inconclusive("an AssumeRole call answered neither allow nor deny")
    if vending.get("with_header") is not True:
        return "fallback", FAIL_BRANCH
    problem = _vending_problem(vending)
    if problem is not None:
        return _verdict_inconclusive(problem)
    controls = [r for r in matrix if r.get("principal") == "lakekeeper"]
    if not controls:
        return _verdict_inconclusive("no positive control ran")
    if any(r.get("observed") != "allow" for r in controls):
        return _verdict_inconclusive("a positive control was not allowed")
    vended = [r for r in matrix if r.get("principal") == "vended"]
    if any(r.get("observed") not in {"allow", "deny"} for r in vended):
        return _verdict_inconclusive("a vended call answered neither allow nor deny")
    if any(r.get("expected") == "allow" and r.get("observed") == "deny" for r in vended):
        return "fallback", FAIL_BRANCH
    if any(r.get("expected") == "deny" and r.get("observed") == "allow" for r in vended):
        return "fallback", WIDE_BRANCH
    return "go", GO_BRANCH


# --- the catalog and the legs -------------------------------------------------------------------------


def _json_or_none(payload: bytes) -> Any:
    try:
        return json.loads(payload)
    except ValueError:
        return None


def _api(
    base: str,
    method: str,
    path: str,
    body: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
) -> tuple[int, Any]:
    request_headers = dict(headers or {})
    data = None
    if body is not None:
        request_headers["Content-Type"] = "application/json"
        data = json.dumps(body).encode("utf-8")
    status, payload = http_request(method, f"{base}{path}", request_headers, data)
    return status, _json_or_none(payload)


def _need(status: int, accepted: tuple[int, ...], step: str) -> None:
    if status not in accepted:
        raise ProbeSetupError(f"{step} (HTTP {status})")


def _text_at(document: Any, *path: str) -> str:
    node = document
    for name in path:
        node = node.get(name) if isinstance(node, Mapping) else None
    if not isinstance(node, str) or not node:
        raise ProbeSetupError(f"response field {'.'.join(path)} is missing")
    return node


def _namespaces_path(catalog_prefix: str) -> str:
    return f"/catalog/v1/{catalog_prefix}/namespaces"


def _drop_catalog(base: str, catalog_prefix: str) -> None:
    """Drop the probe tables (purged) and the namespace. 404 and other statuses are ignored."""
    namespace = f"{_namespaces_path(catalog_prefix)}/{NAMESPACE}"
    for name in (TABLE_A, TABLE_B):
        _api(base, "DELETE", f"{namespace}/tables/{name}?purgeRequested=true")
    _api(base, "DELETE", namespace)


def _load_table(base: str, catalog_prefix: str, name: str, *, delegated: bool) -> Any:
    path = f"{_namespaces_path(catalog_prefix)}/{NAMESPACE}/tables/{name}"
    status, document = _api(base, "GET", path, headers=DELEGATION if delegated else None)
    _need(status, (200,), f"loadTable {name}")
    return document


def _row(
    principal: str,
    target: str,
    operation: str,
    status: int,
    code: str | None,
    expected: str,
    observed: str,
) -> dict[str, Any]:
    return {
        "principal": principal,
        "target": target,
        "operation": operation,
        "status": status,
        "error": code,
        "expected": expected,
        "observed": observed,
        "ok": observed == expected,
    }


def _ok(status: int) -> bool:
    return 200 <= status < 300


def _multipart(client: object, key: str) -> tuple[int, str | None]:
    """A 12 MiB upload in 5 MiB parts; the result is that of the first call that fails.

    A success reply that lacks the upload id or an ETag is reported as status 0, an error.
    """
    where = {"Bucket": BUCKET, "Key": key}
    status, code, created = _call(client, "create_multipart_upload", **where)
    upload_id = created.get("UploadId") if created is not None else None
    if not _ok(status) or not isinstance(upload_id, str):
        return (status if not _ok(status) else 0), code
    last_part = MULTIPART_TOTAL_BYTES - 2 * MULTIPART_PART_BYTES
    sizes = (MULTIPART_PART_BYTES, MULTIPART_PART_BYTES, last_part)
    parts: list[dict[str, Any]] = []
    for number, size in enumerate(sizes, start=1):
        status, code, uploaded = _call(
            client,
            "upload_part",
            **where,
            UploadId=upload_id,
            PartNumber=number,
            Body=bytes(size),
        )
        etag = uploaded.get("ETag") if uploaded is not None else None
        if not _ok(status) or not isinstance(etag, str):
            with contextlib.suppress(Exception):
                _call(client, "abort_multipart_upload", **where, UploadId=upload_id)
            return (status if not _ok(status) else 0), code
        parts.append({"ETag": etag, "PartNumber": number})
    status, code, _ = _call(
        client,
        "complete_multipart_upload",
        **where,
        UploadId=upload_id,
        MultipartUpload={"Parts": parts},
    )
    if not _ok(status):
        with contextlib.suppress(Exception):
            _call(client, "abort_multipart_upload", **where, UploadId=upload_id)
    return status, code


def _execute(client: object, operation: str, keys: Mapping[str, str]) -> tuple[int, str | None]:
    if operation == "put":
        return s3_call(client, "put_object", Bucket=BUCKET, Key=keys["object"], Body=b"probe")
    if operation == "get":
        return s3_call(client, "get_object", Bucket=BUCKET, Key=keys["object"])
    if operation == "get_metadata":
        return s3_call(client, "get_object", Bucket=BUCKET, Key=keys["metadata"])
    if operation == "delete":
        return s3_call(client, "delete_object", Bucket=BUCKET, Key=keys["object"])
    if operation == "list":
        return s3_call(client, "list_objects_v2", Bucket=BUCKET, Prefix=keys["prefix"], MaxKeys=1)
    if operation == "list_prefix_empty":
        return s3_call(client, "list_objects_v2", Bucket=BUCKET, Prefix="", MaxKeys=1)
    if operation == "list_no_prefix":
        return s3_call(client, "list_objects_v2", Bucket=BUCKET, MaxKeys=1)
    if operation == "multipart":
        return _multipart(client, keys["multipart"])
    raise ValueError(f"unknown operation {operation}")


def _seed(client: object, key: str) -> None:
    status, _ = s3_call(client, "put_object", Bucket=BUCKET, Key=key, Body=b"probe-seed")
    _need(status, (200, 201, 204), "seed object")


def _scope_leg(
    base: str,
    catalog_prefix: str,
    control: S3Like,
    read_only: bool,
    touched: list[str],
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    """Create the tables, load them, run the matrix. Returns (vending, matrix rows, observations)."""
    namespaces = _namespaces_path(catalog_prefix)
    status, _ = _api(base, "POST", namespaces, {"namespace": [NAMESPACE]})
    _need(status, (200, 201), "create namespace")
    for name in (TABLE_A, TABLE_B):
        body = {"name": name, "schema": SCHEMA, "properties": {"format-version": "3"}}
        status, _ = _api(base, "POST", f"{namespaces}/{NAMESPACE}/tables", body)
        _need(status, (200, 201), f"create table {name}")
    table_a = _load_table(base, catalog_prefix, TABLE_A, delegated=True)
    table_b = _load_table(base, catalog_prefix, TABLE_B, delegated=True)
    plain_a = _load_table(base, catalog_prefix, TABLE_A, delegated=False)
    config = vended_config(table_a)
    entry = _first_credentials(table_a)
    location_a = _text_at(table_a, "metadata", "location")
    usable = config is not None and all(
        isinstance(config.get(field), str) and config.get(field) for field in VENDED_FIELDS
    )
    prefix = entry.get("prefix") if entry is not None else None
    vending: dict[str, Any] = {
        "with_header": usable,
        "without_header": vended_config(plain_a) is not None,
        "expires_in_s": expires_in_s(config, now()) if config is not None else None,
        "prefix_matches_location": isinstance(prefix, str)
        and _strip_one_slash(prefix) == _strip_one_slash(location_a),
    }
    if config is None or not usable:
        return vending, [], []
    key_a = table_key(location_a)
    key_b = table_key(_text_at(table_b, "metadata", "location"))
    targets: dict[str, dict[str, str]] = {
        "own": {
            "object": f"{key_a}/probe.bin",
            "multipart": f"{key_a}/probe-multipart.bin",
            "prefix": f"{key_a}/",
        },
        "sibling": {
            "object": f"{key_b}/probe-seed.bin",
            "metadata": table_key(_text_at(table_b, "metadata-location")),
            "multipart": f"{key_b}/probe-multipart.bin",
            "prefix": f"{key_b}/",
        },
        "lookalike": {"object": f"{key_a}x/probe.bin"},
        "root": {"object": ROOT_MARKER, "prefix": ""},
    }
    touched.extend(
        keys[name] for keys in targets.values() for name in ("object", "multipart") if name in keys
    )
    for seeded in (targets["own"]["object"], targets["sibling"]["object"], ROOT_MARKER):
        _seed(control, seeded)
    clients: dict[str, object] = {
        "vended": make_s3_client(
            str(config["s3.access-key-id"]),
            str(config["s3.secret-access-key"]),
            str(config["s3.session-token"]),
        ),
        "lakekeeper": control,
    }
    rows: list[dict[str, Any]] = []
    for principal, target, operation, expected in expected_matrix(read_only):
        status, code = _execute(clients[principal], operation, targets[target])
        rows.append(
            _row(principal, target, operation, status, code, expected, classify_s3(status, code))
        )
    vended = clients["vended"]
    unscored = (
        (
            "own",
            "list_no_slash",
            s3_call(vended, "list_objects_v2", Bucket=BUCKET, Prefix=key_a, MaxKeys=1),
        ),
        ("bucket", "list_buckets", s3_call(vended, "list_buckets")),
    )
    observations = [
        {
            "principal": "vended",
            "target": target,
            "operation": operation,
            "status": status,
            "error": code,
            "observed": classify_s3(status, code),
        }
        for target, operation, (status, code) in unscored
    ]
    return vending, rows, observations


def _assume_role_leg(env: Mapping[str, str]) -> list[dict[str, Any]]:
    garbage = (secrets.token_hex(10).upper(), secrets.token_hex(20))
    principals = (
        ("lakekeeper", env["LAKEKEEPER_S3_KEY"], env["LAKEKEEPER_S3_SECRET"], "allow"),
        ("probe-other", env["PROBE_OTHER_KEY"], env["PROBE_OTHER_SECRET"], "deny"),
        ("admin", env["SEAWEEDFS_ADMIN_KEY"], env["SEAWEEDFS_ADMIN_SECRET"], "deny"),
        ("garbage", garbage[0], garbage[1], "deny"),
    )
    rows: list[dict[str, Any]] = []
    for name, access_key_id, secret_access_key, expected in principals:
        client = make_sts_client(access_key_id, secret_access_key)
        status, code = s3_call(
            client, "assume_role", RoleArn=ROLE_ARN, RoleSessionName=ROLE_SESSION_NAME
        )
        rows.append(
            _row(
                name, ROLE_TARGET, "assume_role", status, code, expected, classify_sts(status, code)
            )
        )
    return rows


def _queue_leg(base: str) -> dict[str, Any]:
    status, info = _api(base, "GET", "/management/v1/info")
    info = info if status == 200 and isinstance(info, Mapping) else {}
    status, spec = _api(base, "GET", "/api-docs/management/v1/openapi.json")
    paths = spec.get("paths") if status == 200 and isinstance(spec, Mapping) else None
    queue_paths = sorted(path for path in paths or {} if "task-queue" in path)
    status, listing = _api(base, "GET", "/management/v1/warehouse")
    warehouses = (
        listing.get("warehouses") if status == 200 and isinstance(listing, Mapping) else None
    )
    spike = next(
        (w for w in warehouses or [] if isinstance(w, Mapping) and w.get("name") == WAREHOUSE_NAME),
        {},
    )
    profile = spike.get("delete-profile")
    warehouse_id = spike.get("id")
    route_status: int | None = None
    if isinstance(warehouse_id, str):
        route = f"/management/v1/warehouse/{warehouse_id}/task-queue/{NOT_A_QUEUE}/config"
        route_status, _ = _api(base, "GET", route)
    license_status = info.get("license-status")
    license_type = (
        license_status.get("license-type") if isinstance(license_status, Mapping) else None
    )
    listed = info.get("queues")
    states = classify_queues(info, queue_paths)
    return {
        "version": info.get("version"),
        "license_type": license_type,
        "listed": sorted(name for name in listed if isinstance(name, str))
        if isinstance(listed, list)
        else [],
        "openapi_queue_paths": sorted(_queue_names_in_paths(queue_paths)),
        "expire_snapshots": states["expire_snapshots"],
        "orphan_removal": states["orphan_removal"],
        "delete_profile": profile.get("type") if isinstance(profile, Mapping) else None,
        "any_name_route_status": route_status,
    }


def _cleanup(
    base: str, catalog_prefix: str | None, control: S3Like, touched: Iterable[str]
) -> None:
    """Best effort, in a finally block: the next run pre-cleans whatever this one could not."""
    for key in touched:
        with contextlib.suppress(Exception):
            s3_call(control, "delete_object", Bucket=BUCKET, Key=key)
    if catalog_prefix is not None:
        with contextlib.suppress(Exception):
            _drop_catalog(base, catalog_prefix)


def run_probe(env: Mapping[str, str], read_only: bool) -> dict[str, Any]:
    """Run all four legs against the live stack and return the result without a verdict."""
    values = {name: env[name] for name in REQUIRED_VARIABLES}
    base = env.get("LAKEKEEPER_URL", DEFAULT_LAKEKEEPER_URL)
    control = make_s3_client(values["LAKEKEEPER_S3_KEY"], values["LAKEKEEPER_S3_SECRET"])
    touched = [ROOT_MARKER]
    catalog_prefix: str | None = None
    try:
        status, config = _api(base, "GET", f"/catalog/v1/config?warehouse={WAREHOUSE_NAME}")
        _need(status, (200,), "read the catalog config")
        catalog_prefix = _text_at(config, "defaults", "prefix")
        s3_call(control, "delete_object", Bucket=BUCKET, Key=ROOT_MARKER)
        _drop_catalog(base, catalog_prefix)
        vending, matrix, observations = _scope_leg(
            base, catalog_prefix, control, read_only, touched
        )
        assume_role = _assume_role_leg(values)
        queues = _queue_leg(base)
    finally:
        _cleanup(base, catalog_prefix, control, touched)
    return {
        "read_only": read_only,
        "vending": vending,
        "matrix": matrix,
        "assume_role": assume_role,
        "queues": queues,
        "observations": observations,
    }


# --- output and entry point -----------------------------------------------------------------------------


def _check_line(row: Mapping[str, Any]) -> str:
    error = row["error"] if row["error"] is not None else "-"
    return (
        f"{row['principal']} {row['target']} {row['operation']} status={row['status']} "
        f"error={error} expected={row['expected']} ok={row['ok']}"
    )


def render_text(document: Mapping[str, Any]) -> list[str]:
    lines = [_check_line(row) for row in [*document["matrix"], *document["assume_role"]]]
    for row in document["observations"]:
        error = row["error"] if row["error"] is not None else "-"
        lines.append(
            f"observation {row['principal']} {row['target']} {row['operation']} "
            f"status={row['status']} error={error} observed={row['observed']}"
        )
    vending = document["vending"]
    lines.append("vending " + " ".join(f"{key}={vending[key]}" for key in vending))
    queues = document["queues"]
    lines.append("queues " + " ".join(f"{key}={queues[key]}" for key in queues))
    lines.append(f"verdict: {document['verdict']} ({document['branch']})")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("--json", action="store_true", help="print one compact JSON line")
    parser.add_argument(
        "--read-only", action="store_true", help="expect own-prefix writes to be denied"
    )
    args = parser.parse_args(argv)
    for name in AMBIENT_AWS_VARIABLES:
        if name in os.environ:
            print(f"refusing to run: the ambient AWS variable {name} is set", file=sys.stderr)
            return 2
    try:
        result = run_probe(os.environ, args.read_only)
    except KeyError as exc:
        print(f"missing environment variable: {exc.args[0]}", file=sys.stderr)
        return 2
    except ProbeSetupError as exc:
        print(f"probe setup failed: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"request failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"probe failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    outcome, branch = verdict(result)
    document = {"verdict": outcome, "branch": branch, **result}
    if args.json:
        print(json.dumps(document, separators=(",", ":")))
    else:
        print("\n".join(render_text(document)))
    return 0 if outcome == "go" else 1


if __name__ == "__main__":
    sys.exit(main())
