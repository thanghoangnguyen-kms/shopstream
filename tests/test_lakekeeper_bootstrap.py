"""Unit tests for scripts/lakekeeper_bootstrap.py. No network: http_request is replaced."""

from __future__ import annotations

import hashlib
import json
import secrets
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from email.message import Message
from io import BytesIO

import lakekeeper_bootstrap as lb
import pytest

EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()

# AWS's documented "GET Object" example (Signature Version 4, header-based auth). The example
# key pair is assembled from fragments so no provider-shaped literal sits in the file.
AWS_DOC_ID = "AKIA" + "IOSFODNN7EXAMPLE"
AWS_DOC_MATERIAL = "".join(["wJalrXUtn", "FEMI/K7MDENG/", "bPxRfiCY", "EXAMPLEKEY"])
AWS_EXAMPLE_SIGNATURE = "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (200, None, "created"),
        (409, "BucketAlreadyOwnedByYou", "exists"),
        (409, "BucketAlreadyExists", "exists"),
        (403, "AccessDenied", "failed"),
        (403, "SignatureDoesNotMatch", "failed"),
        (409, "OperationAborted", "failed"),
        (500, None, "failed"),
    ],
)
def test_classify_bucket(status: int, code: str | None, expected: str) -> None:
    assert lb.classify_bucket(status, code) == expected


@pytest.mark.parametrize(
    ("status", "error_type", "expected"),
    [
        (200, None, "created"),
        (204, None, "created"),
        (400, "CatalogAlreadyBootstrapped", "exists"),
        (400, "ConcurrentBootstrap", "exists"),
        (400, "TermsOfUseNotAccepted", "failed"),
        (409, "CatalogAlreadyBootstrapped", "failed"),
        (500, None, "failed"),
    ],
)
def test_classify_bootstrap(status: int, error_type: str | None, expected: str) -> None:
    assert lb.classify_bootstrap(status, error_type) == expected


@pytest.mark.parametrize(
    ("status", "error_type", "message", "expected"),
    [
        (201, None, None, "created"),
        (409, "WarehouseAlreadyExists", None, "exists"),
        (409, "SomethingElse", None, "failed"),
        (
            400,
            "CreateWarehouseStorageProfileOverlap",
            "Storage profile overlaps with existing warehouse spike",
            "exists",
        ),
        (
            400,
            "CreateWarehouseStorageProfileOverlap",
            "Storage profile overlaps with existing warehouse other",
            "failed",
        ),
        (400, "CreateWarehouseStorageProfileOverlap", None, "failed"),
        (403, "AccessDenied", None, "failed"),
        (500, None, None, "failed"),
    ],
)
def test_classify_warehouse(
    status: int, error_type: str | None, message: str | None, expected: str
) -> None:
    assert lb.classify_warehouse(status, error_type, message) == expected


def test_parse_lakekeeper_error_reads_the_envelope() -> None:
    body = json.dumps({"error": {"type": "WarehouseAlreadyExists", "message": "nope", "code": 409}})
    assert lb.parse_lakekeeper_error(body.encode()) == ("WarehouseAlreadyExists", "nope")


@pytest.mark.parametrize(
    "body", [b"", b"not json", b"[]", b'{"error": "text"}', b'{"error": {"type": 3}}']
)
def test_parse_lakekeeper_error_tolerates_garbage(body: bytes) -> None:
    error_type, _ = lb.parse_lakekeeper_error(body)
    assert error_type is None


def test_parse_s3_error_reads_code_and_message() -> None:
    body = b"<Error><Code>BucketAlreadyOwnedByYou</Code><Message>mine</Message></Error>"
    assert lb.parse_s3_error(body) == ("BucketAlreadyOwnedByYou", "mine")


@pytest.mark.parametrize("body", [b"", b"plain text", b"\xff\xfe"])
def test_parse_s3_error_tolerates_garbage(body: bytes) -> None:
    assert lb.parse_s3_error(body) == (None, None)


def _walk_keys(node: object) -> list[str]:
    if isinstance(node, dict):
        return [*node, *(key for child in node.values() for key in _walk_keys(child))]
    if isinstance(node, list):
        return [key for child in node for key in _walk_keys(child)]
    return []


def _env() -> dict[str, str]:
    return {
        "LAKEKEEPER_S3_KEY": secrets.token_hex(8),
        "LAKEKEEPER_S3_SECRET": secrets.token_hex(16),
    }


def test_warehouse_body_matches_the_spike_profile() -> None:
    env = _env()
    body = lb.warehouse_body(env)
    assert body["warehouse-name"] == "spike"
    assert body["storage-profile"] == {
        "type": "s3",
        "bucket": "warehouse",
        "endpoint": "http://seaweedfs:8333",
        "sts-endpoint": "http://seaweedfs:8333",
        "sts-role-arn": "arn:aws:iam::000000000000:role/LakekeeperVendedRole",
        "region": "local-01",
        "path-style-access": True,
        "flavor": "s3-compat",
        "sts-enabled": True,
        "sts-token-validity-seconds": 3600,
    }
    assert body["storage-credential"] == {
        "type": "s3",
        "credential-type": "access-key",
        "access-key-id": env["LAKEKEEPER_S3_KEY"],
        "secret-access-key": env["LAKEKEEPER_S3_SECRET"],
    }
    assert body["delete-profile"] == {"type": "hard"}


def test_warehouse_body_sets_no_format_version_keys() -> None:
    keys = _walk_keys(lb.warehouse_body(_env()))
    assert not [key for key in keys if "format-version" in key]


@pytest.mark.parametrize("name", ["LAKEKEEPER_S3_KEY", "LAKEKEEPER_S3_SECRET"])
@pytest.mark.parametrize("empty", [False, True])
def test_warehouse_body_names_a_missing_variable(name: str, empty: bool) -> None:
    env = _env()
    if empty:
        env[name] = ""
    else:
        del env[name]
    with pytest.raises(KeyError, match=name):
        lb.warehouse_body(env)


def test_sigv4_reproduces_the_aws_get_object_example() -> None:
    authorization = lb.sigv4_authorization(
        "GET",
        "examplebucket.s3.amazonaws.com",
        "/test.txt",
        {"Range": "bytes=0-9", "x-amz-content-sha256": EMPTY_SHA256},
        EMPTY_SHA256,
        AWS_DOC_ID,
        AWS_DOC_MATERIAL,
        "us-east-1",
        "20130524T000000Z",
    )
    assert authorization == (
        f"AWS4-HMAC-SHA256 Credential={AWS_DOC_ID}/20130524/us-east-1/s3/aws4_request,"
        f"SignedHeaders=host;range;x-amz-content-sha256;x-amz-date,Signature={AWS_EXAMPLE_SIGNATURE}"
    )


def test_sigv4_ignores_header_name_case_and_order() -> None:
    args = (EMPTY_SHA256, AWS_DOC_ID, AWS_DOC_MATERIAL, "us-east-1", "20130524T000000Z")
    first = lb.sigv4_authorization(
        "GET", "h", "/x", {"Range": "bytes=0-9", "x-amz-content-sha256": EMPTY_SHA256}, *args
    )
    second = lb.sigv4_authorization(
        "GET", "h", "/x", {"X-Amz-Content-SHA256": EMPTY_SHA256, "range": "bytes=0-9"}, *args
    )
    assert first == second


@pytest.mark.parametrize(
    "url",
    ["http://lakekeeper:8181/management/v1/info", "http://seaweedfs:8333/warehouse"],
)
def test_check_url_allows_the_in_network_origins(url: str) -> None:
    lb.check_url(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://lakekeeper:8181/x",
        "http://evil.example/x",
        "http://lakekeeper:9999/x",
        "http://lakekeeper:8181@evil.example/x",
        "http://lakekeeper:8181.evil.example/x",
        "ftp://lakekeeper:8181/x",
        "lakekeeper:8181/x",
    ],
)
def test_check_url_rejects_everything_else(url: str) -> None:
    with pytest.raises(ValueError, match="allowed"):
        lb.check_url(url)


def test_http_request_refuses_a_bad_url_before_any_network_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    with pytest.raises(ValueError, match="allowed"):
        lb.http_request("GET", "https://lakekeeper:8181/x", {}, None)


def test_http_request_returns_the_status_of_an_http_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*args: object, **kwargs: object) -> object:
        raise urllib.error.HTTPError(
            "http://lakekeeper:8181/x", 409, "conflict", Message(), BytesIO(b"payload")
        )

    monkeypatch.setattr(urllib.request, "urlopen", refuse)
    assert lb.http_request("POST", "http://lakekeeper:8181/x", {}, b"{}") == (409, b"payload")


@dataclass
class Call:
    method: str
    url: str
    headers: Mapping[str, str]
    body: bytes | None


@dataclass
class Script:
    """Scripted HTTP responses, consumed in order, with every request recorded."""

    responses: list[tuple[int, bytes]]
    calls: list[Call] = field(default_factory=list)

    def __call__(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        self.calls.append(Call(method, url, headers, body))
        return self.responses.pop(0)


def _lk_error(status: int, error_type: str, message: str = "m") -> tuple[int, bytes]:
    return status, json.dumps({"error": {"type": error_type, "message": message}}).encode()


def _s3_error(status: int, code: str) -> tuple[int, bytes]:
    return status, f"<Error><Code>{code}</Code><Message>m</Message></Error>".encode()


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    values = {
        "SEAWEEDFS_ADMIN_KEY": secrets.token_hex(8),
        "SEAWEEDFS_ADMIN_SECRET": secrets.token_hex(16),
        "LAKEKEEPER_S3_KEY": secrets.token_hex(8),
        "LAKEKEEPER_S3_SECRET": secrets.token_hex(16),
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("LAKEKEEPER_URL", raising=False)
    return values


def test_bootstrap_creates_the_bucket_and_bootstraps_the_catalog(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    script = Script([(200, b""), (204, b"")])
    monkeypatch.setattr(lb, "http_request", script)
    assert lb.main(["bootstrap"]) == 0
    out = capsys.readouterr().out
    assert "bucket warehouse: created" in out
    assert "catalog bootstrap: created" in out
    put, post = script.calls
    assert (put.method, put.url) == ("PUT", "http://seaweedfs:8333/warehouse")
    assert put.headers["Authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential={env['SEAWEEDFS_ADMIN_KEY']}/"
    )
    assert env["SEAWEEDFS_ADMIN_SECRET"] not in json.dumps(dict(put.headers))
    assert (post.method, post.url) == ("POST", "http://lakekeeper:8181/management/v1/bootstrap")
    assert post.body is not None
    assert json.loads(post.body) == {"accept-terms-of-use": True}


@pytest.mark.parametrize("race", ["CatalogAlreadyBootstrapped", "ConcurrentBootstrap"])
def test_bootstrap_rerun_and_race_converge_to_exists(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    race: str,
) -> None:
    script = Script([_s3_error(409, "BucketAlreadyOwnedByYou"), _lk_error(400, race)])
    monkeypatch.setattr(lb, "http_request", script)
    assert lb.main(["bootstrap"]) == 0
    out = capsys.readouterr().out
    assert "bucket warehouse: exists" in out
    assert "catalog bootstrap: exists" in out


def test_bootstrap_stops_when_the_bucket_step_fails(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    script = Script([_s3_error(403, "AccessDenied")])
    monkeypatch.setattr(lb, "http_request", script)
    assert lb.main(["bootstrap"]) == 1
    assert "bucket warehouse failed: status=403 type=AccessDenied" in capsys.readouterr().out
    assert len(script.calls) == 1


@pytest.mark.parametrize(
    ("response", "verdict"),
    [
        ((201, b"{}"), "created"),
        (_lk_error(409, "WarehouseAlreadyExists"), "exists"),
        (
            _lk_error(
                400,
                "CreateWarehouseStorageProfileOverlap",
                "Storage profile overlaps with existing warehouse spike",
            ),
            "exists",
        ),
    ],
)
def test_warehouse_reports_created_or_exists(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
    response: tuple[int, bytes],
    verdict: str,
) -> None:
    script = Script([response])
    monkeypatch.setattr(lb, "http_request", script)
    assert lb.main(["warehouse"]) == 0
    assert f"warehouse spike: {verdict}" in capsys.readouterr().out
    (call,) = script.calls
    assert call.url == "http://lakekeeper:8181/management/v1/warehouse"
    assert call.body is not None
    assert json.loads(call.body)["storage-credential"]["access-key-id"] == env["LAKEKEEPER_S3_KEY"]


def test_a_failed_step_prints_status_type_and_message_only(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(lb, "http_request", Script([_lk_error(500, "InternalError", "boom")]))
    assert lb.main(["warehouse"]) == 1
    out = capsys.readouterr().out
    assert "warehouse spike failed: status=500 type=InternalError message=boom" in out
    for secret in (env["LAKEKEEPER_S3_KEY"], env["LAKEKEEPER_S3_SECRET"], "storage-credential"):
        assert secret not in out


def test_a_missing_variable_is_a_config_error_before_any_request(
    monkeypatch: pytest.MonkeyPatch,
    env: dict[str, str],
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.delenv("LAKEKEEPER_S3_SECRET")
    script = Script([])
    monkeypatch.setattr(lb, "http_request", script)
    assert lb.main(["warehouse"]) == 2
    assert "LAKEKEEPER_S3_SECRET" in capsys.readouterr().err
    assert script.calls == []


def test_a_disallowed_lakekeeper_url_is_a_config_error(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def no_network(*args: object, **kwargs: object) -> object:
        raise AssertionError("network call attempted")

    monkeypatch.setattr(urllib.request, "urlopen", no_network)
    monkeypatch.setenv("LAKEKEEPER_URL", "https://evil.example")
    assert lb.main(["warehouse"]) == 2
    assert "allowed" in capsys.readouterr().err


def test_a_network_error_fails_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str], capsys: pytest.CaptureFixture[str]
) -> None:
    def down(*args: object, **kwargs: object) -> tuple[int, bytes]:
        raise ConnectionRefusedError

    monkeypatch.setattr(lb, "http_request", down)
    assert lb.main(["warehouse"]) == 1
    assert "ConnectionRefusedError" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["nonsense"], ["bootstrap", "warehouse"]])
def test_usage_errors_exit_2(argv: list[str]) -> None:
    assert lb.main(argv) == 2
