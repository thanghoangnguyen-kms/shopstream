"""Bootstrap Lakekeeper and create the spike warehouse. Run by `just up` inside a container.

The `bootstrap` and `warehouse` one-shots in infra/compose.yaml bind-mount this file into
python:3.13-slim, where nothing is installed, so it uses the standard library only and
imports no sibling module. Every step is idempotent: a second run reports `exists` and exits 0.

Output is one status line per step. On failure it prints the step, the HTTP status and the
error type and message from the response, never a request body, a key or a response body.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime

# The one value Phase 2 flips for host access. On Colima, host.docker.internal resolves inside
# containers but not on the macOS host (measured 2026-09-30), so Phase 1 stays in-network.
S3_ENDPOINT = "http://seaweedfs:8333"
BUCKET = "warehouse"
WAREHOUSE_NAME = "spike"
REGION = "local-01"
ROLE_ARN = "arn:aws:iam::000000000000:role/LakekeeperVendedRole"
STS_TOKEN_VALIDITY_S = 3600
TIMEOUT_S = 30
ALLOWED_ORIGINS = frozenset({"http://lakekeeper:8181", "http://seaweedfs:8333"})
DEFAULT_LAKEKEEPER_URL = "http://lakekeeper:8181"

EMPTY_PAYLOAD_SHA256 = hashlib.sha256(b"").hexdigest()
_S3_CODE = re.compile(r"<Code>([^<]*)</Code>")
_S3_MESSAGE = re.compile(r"<Message>([^<]*)</Message>")


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


def classify_bucket(status: int, code: str | None) -> str:
    if 200 <= status < 300:
        return "created"
    if status == 409 and code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
        return "exists"
    return "failed"


def classify_bootstrap(status: int, error_type: str | None) -> str:
    if 200 <= status < 300:
        return "created"
    if status == 400 and error_type in {"CatalogAlreadyBootstrapped", "ConcurrentBootstrap"}:
        return "exists"
    return "failed"


def classify_warehouse(status: int, error_type: str | None, message: str | None = None) -> str:
    if 200 <= status < 300:
        return "created"
    if status == 409 and error_type == "WarehouseAlreadyExists":
        return "exists"
    # Lakekeeper 0.13.6 checks storage overlap before the name, so re-creating the same
    # warehouse answers 400 with this type and names the warehouse it overlaps. Only an
    # overlap with our own warehouse counts as exists; a different name is a real failure.
    if (
        status == 400
        and error_type == "CreateWarehouseStorageProfileOverlap"
        and message is not None
        and message.rstrip(". ").endswith(f"existing warehouse {WAREHOUSE_NAME}")
    ):
        return "exists"
    return "failed"


def parse_lakekeeper_error(body: bytes) -> tuple[str | None, str | None]:
    """Return (type, message) from Lakekeeper's `{"error": {...}}` envelope."""
    try:
        document = json.loads(body)
    except ValueError:
        return None, None
    if not isinstance(document, dict):
        return None, None
    error = document.get("error")
    if not isinstance(error, dict):
        return None, None
    error_type = error.get("type")
    message = error.get("message")
    return (
        error_type if isinstance(error_type, str) else None,
        message if isinstance(message, str) else None,
    )


def parse_s3_error(body: bytes) -> tuple[str | None, str | None]:
    """Return (Code, Message) from an S3 XML error body, without an XML parser."""
    text = body.decode("utf-8", errors="replace")
    code = _S3_CODE.search(text)
    message = _S3_MESSAGE.search(text)
    return (code.group(1) if code else None, message.group(1) if message else None)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def sigv4_authorization(
    method: str,
    host: str,
    path: str,
    headers: Mapping[str, str],
    payload_hash: str,
    access_key: str,
    secret_key: str,
    region: str,
    amz_date: str,
    service: str = "s3",
) -> str:
    """Build an AWS Signature Version 4 Authorization header value (no query string)."""
    signed = {name.lower(): " ".join(value.split()) for name, value in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_uri = urllib.parse.quote(path, safe="/-_.~")
    canonical_request = "\n".join(
        [method, canonical_uri, "", canonical_headers, signed_headers, payload_hash]
    )
    date = amz_date[:8]
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ]
    )
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    signature = hmac.new(key, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    return (
        f"AWS4-HMAC-SHA256 Credential={access_key}/{scope},"
        f"SignedHeaders={signed_headers},Signature={signature}"
    )


def warehouse_body(env: Mapping[str, str]) -> dict[str, object]:
    """Build the create-warehouse request in memory. It carries credentials: never print it."""
    for name in ("LAKEKEEPER_S3_KEY", "LAKEKEEPER_S3_SECRET"):
        if not env.get(name):
            raise KeyError(name)
    return {
        "warehouse-name": WAREHOUSE_NAME,
        "storage-profile": {
            "type": "s3",
            "bucket": BUCKET,
            "endpoint": S3_ENDPOINT,
            "sts-endpoint": S3_ENDPOINT,
            "sts-role-arn": ROLE_ARN,
            "region": REGION,
            "path-style-access": True,
            "flavor": "s3-compat",
            "sts-enabled": True,
            "sts-token-validity-seconds": STS_TOKEN_VALIDITY_S,
        },
        "storage-credential": {
            "type": "s3",
            "credential-type": "access-key",
            "access-key-id": env["LAKEKEEPER_S3_KEY"],
            "secret-access-key": env["LAKEKEEPER_S3_SECRET"],
        },
        "delete-profile": {"type": "hard"},
    }


def _fail(step: str, status: int, error_type: str | None, message: str | None) -> int:
    print(f"{step} failed: status={status} type={error_type} message={message}")
    return 1


def ensure_bucket(env: Mapping[str, str]) -> int:
    for name in ("SEAWEEDFS_ADMIN_KEY", "SEAWEEDFS_ADMIN_SECRET"):
        if not env.get(name):
            raise KeyError(name)
    host = urllib.parse.urlsplit(S3_ENDPOINT).netloc
    path = f"/{BUCKET}"
    amz_date = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    signed_headers = {"x-amz-content-sha256": EMPTY_PAYLOAD_SHA256}
    authorization = sigv4_authorization(
        "PUT",
        host,
        path,
        signed_headers,
        EMPTY_PAYLOAD_SHA256,
        env["SEAWEEDFS_ADMIN_KEY"],
        env["SEAWEEDFS_ADMIN_SECRET"],
        REGION,
        amz_date,
    )
    request_headers = {
        **signed_headers,
        "x-amz-date": amz_date,
        "Authorization": authorization,
    }
    status, body = http_request("PUT", f"{S3_ENDPOINT}{path}", request_headers, None)
    code, message = parse_s3_error(body)
    verdict = classify_bucket(status, code)
    if verdict == "failed":
        return _fail(f"bucket {BUCKET}", status, code, message)
    print(f"bucket {BUCKET}: {verdict}")
    return 0


def bootstrap_catalog(env: Mapping[str, str]) -> int:
    base = env.get("LAKEKEEPER_URL", DEFAULT_LAKEKEEPER_URL)
    body = json.dumps({"accept-terms-of-use": True}).encode("utf-8")
    status, response = http_request(
        "POST",
        f"{base}/management/v1/bootstrap",
        {"Content-Type": "application/json"},
        body,
    )
    error_type, message = parse_lakekeeper_error(response)
    verdict = classify_bootstrap(status, error_type)
    if verdict == "failed":
        return _fail("catalog bootstrap", status, error_type, message)
    print(f"catalog bootstrap: {verdict}")
    return 0


def create_warehouse(env: Mapping[str, str]) -> int:
    base = env.get("LAKEKEEPER_URL", DEFAULT_LAKEKEEPER_URL)
    body = json.dumps(warehouse_body(env)).encode("utf-8")
    status, response = http_request(
        "POST",
        f"{base}/management/v1/warehouse",
        {"Content-Type": "application/json"},
        body,
    )
    error_type, message = parse_lakekeeper_error(response)
    verdict = classify_warehouse(status, error_type, message)
    if verdict == "failed":
        return _fail(f"warehouse {WAREHOUSE_NAME}", status, error_type, message)
    print(f"warehouse {WAREHOUSE_NAME}: {verdict}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in {"bootstrap", "warehouse"}:
        print("usage: lakekeeper_bootstrap.py bootstrap|warehouse", file=sys.stderr)
        return 2
    env = os.environ
    try:
        if args[0] == "bootstrap":
            return ensure_bucket(env) or bootstrap_catalog(env)
        return create_warehouse(env)
    except KeyError as exc:
        print(f"missing environment variable: {exc.args[0]}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"request failed: {type(exc).__name__}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
