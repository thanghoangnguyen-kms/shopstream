"""Unit tests for scripts/probe_scope.py. No Docker, no boto3: every seam is replaced by a fake.

The fakes model a small world: a bucket of keys, a Lakekeeper catalog that vends one credential
per table, and a vended credential that reaches only its own table's prefix. The catalog can also
vend without the delegation header, as Lakekeeper 0.13.6 does. Fake secrets come from
secrets.token_hex so nothing credential-shaped sits in the file.
"""

from __future__ import annotations

import copy
import json
import secrets
from collections.abc import Callable, Iterator, Mapping
from typing import Any

import probe_scope as ps
import pytest
import redact_evidence

NOW = 1_700_000_000.0
WAREHOUSE_ID = "0190wh"
UUIDS = {"a": "0190aaaa", "b": "0190bbbb"}
INFO: dict[str, Any] = {
    "version": "0.13.6",
    "queues": ["tabular_expiration", "tabular_purge", "task_log_cleanup"],
    "license-status": {"license-type": "Apache-2.0", "valid": True},
}
OPENAPI_PATHS = [
    "/management/v1/warehouse/{warehouse_id}/task-queue/tabular_expiration/config",
    "/management/v1/warehouse/{warehouse_id}/task-queue/tabular_purge/config",
    "/management/v1/project/task-queue/task_log_cleanup/config",
    "/management/v1/warehouse/{warehouse_id}/task/list",
]
AMBIENT = ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_PROFILE")


# --- fakes --------------------------------------------------------------------------------------


class FakeClientError(Exception):
    """Shaped like botocore's ClientError: a `response` dict, and a message that holds nothing."""

    def __init__(self, status: int, code: str | None = None) -> None:
        super().__init__("fake client error")
        self.response: dict[str, Any] = {"ResponseMetadata": {"HTTPStatusCode": status}}
        if code is not None:
            self.response["Error"] = {"Code": code}


class World:
    """The bucket, the catalog and every credential the probe will be handed."""

    def __init__(
        self,
        *,
        vended_read_only: bool = False,
        vended_reaches_all: bool = False,
        vends_without_header: bool = False,
        no_header_prefix: str | None = None,
    ) -> None:
        self.vended_read_only = vended_read_only
        self.vended_reaches_all = vended_reaches_all
        self.vends_without_header = vends_without_header
        self.no_header_prefix = no_header_prefix
        self.objects: set[str] = set()
        self.tables: dict[str, str] = {}
        self.namespace = False
        self.http_log: list[tuple[str, str]] = []
        self.s3_log: list[tuple[str, str, str]] = []
        self.sts_log: list[str] = []
        self.handed_out: set[str] = set()
        self.control_pair = (secrets.token_hex(8), secrets.token_hex(16))
        self.other_pair = (secrets.token_hex(8), secrets.token_hex(16))
        self.admin_pair = (secrets.token_hex(8), secrets.token_hex(16))
        self.vended_pair = (secrets.token_hex(8), secrets.token_hex(16))
        self.vended_session = secrets.token_hex(24)
        self.fail_on: str | None = None
        self.handed_out.update(
            [*self.control_pair, *self.other_pair, *self.admin_pair, *self.vended_pair]
        )
        self.handed_out.add(self.vended_session)

    def env(self) -> dict[str, str]:
        return {
            "LAKEKEEPER_S3_KEY": self.control_pair[0],
            "LAKEKEEPER_S3_SECRET": self.control_pair[1],
            "PROBE_OTHER_KEY": self.other_pair[0],
            "PROBE_OTHER_SECRET": self.other_pair[1],
            "SEAWEEDFS_ADMIN_KEY": self.admin_pair[0],
            "SEAWEEDFS_ADMIN_SECRET": self.admin_pair[1],
        }

    def leave_leftovers(self) -> None:
        """What an interrupted earlier run leaves behind."""
        self.namespace = True
        self.tables = {"a": "0190oldaa", "b": "0190oldbb"}
        self.objects.add(ps.ROOT_MARKER)

    # --- the HTTP seam ---

    def http(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, bytes]:
        path = url.split("8181", 1)[1]
        self.http_log.append((method, path))
        if self.fail_on and self.fail_on in path:
            raise OSError("boom")
        status, document = self._route(method, path, headers, body)
        return status, b"" if document is None else json.dumps(document).encode("utf-8")

    def _route(
        self, method: str, path: str, headers: Mapping[str, str], body: bytes | None
    ) -> tuple[int, dict[str, Any] | None]:
        base = f"/catalog/v1/{WAREHOUSE_ID}/namespaces"
        if path == "/catalog/v1/config?warehouse=spike":
            return 200, {"defaults": {"prefix": WAREHOUSE_ID}}
        if method == "DELETE" and path.startswith(f"{base}/probe_scope/tables/"):
            name = path.split("/tables/")[1].split("?")[0]
            return (204, None) if self.tables.pop(name, None) else (404, None)
        if method == "DELETE" and path == f"{base}/probe_scope":
            if self.tables:
                return 409, {"error": {"type": "NamespaceNotEmpty"}}
            found, self.namespace = self.namespace, False
            return (204, None) if found else (404, None)
        if method == "POST" and path == base:
            if self.namespace:
                return 409, {"error": {"type": "NamespaceAlreadyExists"}}
            self.namespace = True
            return 200, {}
        if method == "POST" and path == f"{base}/probe_scope/tables":
            assert body is not None
            self.create_table(json.loads(body)["name"])
            return 200, {}
        if method == "GET" and path.startswith(f"{base}/probe_scope/tables/"):
            return self._load_table(path.rsplit("/", 1)[1], headers)
        return self._management(path)

    def create_table(self, name: str) -> None:
        if name in self.tables:
            raise AssertionError(f"table {name} already exists: leftovers were not dropped")
        self.tables[name] = UUIDS[name]
        self.objects.add(f"{UUIDS[name]}/metadata/00000.metadata.json")

    def _load_table(self, name: str, headers: Mapping[str, str]) -> tuple[int, dict[str, Any]]:
        if name not in self.tables:
            return 404, {}
        location = f"s3://warehouse/{self.tables[name]}"
        document: dict[str, Any] = {
            "metadata-location": f"{location}/metadata/00000.metadata.json",
            "metadata": {"location": location},
            "config": {},
        }
        delegated = headers.get("X-Iceberg-Access-Delegation") == "vended-credentials"
        if delegated or self.vends_without_header:
            expires = str(int((NOW + 3600) * 1000))
            prefix = (
                location if delegated or self.no_header_prefix is None else self.no_header_prefix
            )
            document["storage-credentials"] = [
                {
                    "prefix": prefix,
                    "config": {
                        "s3.access-key-id": self.vended_pair[0],
                        "s3.secret-access-key": self.vended_pair[1],
                        "s3.session-token": self.vended_session,
                        "s3.session-token-expires-at-ms": expires,
                    },
                }
            ]
        if self.vends_without_header and not delegated:
            document["config"] = {
                "s3.access-key-id": self.vended_pair[0],
                "s3.secret-access-key": self.vended_pair[1],
                "s3.session-token": self.vended_session,
            }
        return 200, document

    def _management(self, path: str) -> tuple[int, dict[str, Any] | None]:
        if path == "/management/v1/info":
            return 200, INFO
        if path == "/api-docs/management/v1/openapi.json":
            return 200, {"paths": {path: {} for path in OPENAPI_PATHS}}
        if path == "/management/v1/warehouse":
            profile = {"type": "hard"}
            return 200, {
                "warehouses": [{"id": WAREHOUSE_ID, "name": "spike", "delete-profile": profile}]
            }
        if path.startswith(f"/management/v1/warehouse/{WAREHOUSE_ID}/task-queue/"):
            return 200, {"queue-config": {}}
        return 404, None

    # --- the S3 and STS seams ---

    def make_s3(
        self, access_key_id: str, secret_access_key: str, session_token: str | None = None
    ) -> FakeS3:
        self.handed_out.update([access_key_id, secret_access_key])
        if session_token:
            self.handed_out.add(session_token)
        return FakeS3("control" if access_key_id == self.control_pair[0] else "vended", self)

    def make_sts(self, access_key_id: str, secret_access_key: str) -> FakeSts:
        self.handed_out.update([access_key_id, secret_access_key])
        return FakeSts(access_key_id, self)


class FakeS3:
    """boto3's S3 client surface the probe uses, answering from the World."""

    def __init__(self, principal: str, world: World) -> None:
        self.principal = principal
        self.world = world

    def _answer(self, operation: str, key: str, *, listing: bool = False) -> dict[str, Any]:
        world = self.world
        world.s3_log.append((self.principal, operation, key))
        if self.principal == "vended" and not self._vended_may(operation, key, listing):
            raise FakeClientError(403, "AccessDenied")
        if not listing and operation in {"get_object"} and key not in world.objects:
            raise FakeClientError(404, "NoSuchKey")
        if operation == "put_object" or operation == "complete_multipart_upload":
            world.objects.add(key)
        if operation == "delete_object":
            world.objects.discard(key)
        status = 204 if operation == "delete_object" else 200
        return {"ResponseMetadata": {"HTTPStatusCode": status}}

    def _vended_may(self, operation: str, key: str, listing: bool) -> bool:
        if operation == "list_buckets" or self.world.vended_reaches_all:
            return True
        if self.world.vended_read_only and operation not in {"get_object", "list_objects_v2"}:
            return False
        own = UUIDS["a"]
        return key == f"{own}/" if listing else key.startswith(f"{own}/")

    def put_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("put_object", kwargs["Key"])

    def get_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("get_object", kwargs["Key"])

    def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("delete_object", kwargs["Key"])

    def list_objects_v2(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("list_objects_v2", kwargs.get("Prefix", "<none>"), listing=True)

    def list_buckets(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("list_buckets", "", listing=True)

    def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        answer = self._answer("create_multipart_upload", kwargs["Key"])
        return {**answer, "UploadId": "upload-1"}

    def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        answer = self._answer("upload_part", kwargs["Key"])
        return {**answer, "ETag": f"etag-{kwargs['PartNumber']}"}

    def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("complete_multipart_upload", kwargs["Key"])

    def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        return self._answer("abort_multipart_upload", kwargs["Key"])


class FakeSts:
    def __init__(self, access_key_id: str, world: World) -> None:
        self.access_key_id = access_key_id
        self.world = world

    def assume_role(self, **kwargs: Any) -> dict[str, Any]:
        assert kwargs["RoleArn"] == ps.ROLE_ARN
        self.world.sts_log.append(self.access_key_id)
        if self.access_key_id == self.world.control_pair[0]:
            return {"ResponseMetadata": {"HTTPStatusCode": 200}, "Credentials": {"x": "y"}}
        if self.access_key_id in {self.world.other_pair[0], self.world.admin_pair[0]}:
            raise FakeClientError(403, "AccessDenied")
        raise FakeClientError(403)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    fake = World()
    install(monkeypatch, fake)
    return fake


def install(monkeypatch: pytest.MonkeyPatch, fake: World) -> None:
    monkeypatch.setattr(ps, "http_request", fake.http)
    monkeypatch.setattr(ps, "make_s3_client", fake.make_s3)
    monkeypatch.setattr(ps, "make_sts_client", fake.make_sts)
    monkeypatch.setattr(ps, "now", lambda: NOW)
    for name in AMBIENT:
        monkeypatch.delenv(name, raising=False)
    for name, value in fake.env().items():
        monkeypatch.setenv(name, value)


def run_main(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = ps.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def keys_of(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, inner in value.items():
            yield str(key)
            yield from keys_of(inner)
    elif isinstance(value, list):
        for inner in value:
            yield from keys_of(inner)


# --- result builders for verdict() ---------------------------------------------------------------


def matrix_row(principal: str, target: str, operation: str, expected: str) -> dict[str, Any]:
    allowed = expected == "allow"
    return {
        "principal": principal,
        "target": target,
        "operation": operation,
        "status": 200 if allowed else 403,
        "error": None if allowed else "AccessDenied",
        "expected": expected,
        "observed": expected,
        "ok": True,
    }


def assume_row(principal: str, expected: str) -> dict[str, Any]:
    return matrix_row(principal, "LakekeeperVendedRole", "assume_role", expected)


def good_result(*, read_only: bool = False) -> dict[str, Any]:
    return {
        "read_only": read_only,
        "vending": {
            "with_header": True,
            "without_header": False,
            "without_header_prefix_matches_location": None,
            "without_header_config_vends": False,
            "expires_in_s": 3600,
            "prefix_matches_location": True,
        },
        "matrix": [matrix_row(*row) for row in ps.expected_matrix(read_only)],
        "assume_role": [
            assume_row("lakekeeper", "allow"),
            assume_row("probe-other", "deny"),
            assume_row("admin", "deny"),
            assume_row("garbage", "deny"),
        ],
    }


def mutate_row(
    result: dict[str, Any], leg: str, match: Callable[[dict[str, Any]], bool], **changes: Any
) -> dict[str, Any]:
    changed = copy.deepcopy(result)
    for row in changed[leg]:
        if match(row):
            row.update(changes)
            row["ok"] = row["observed"] == row["expected"]
            return changed
    raise AssertionError("no row matched")


def vended_row(target: str, operation: str) -> Callable[[dict[str, Any]], bool]:
    return lambda row: (
        row["principal"] == "vended" and row["target"] == target and row["operation"] == operation
    )


def control_row(target: str, operation: str) -> Callable[[dict[str, Any]], bool]:
    return lambda row: (
        row["principal"] == "lakekeeper"
        and row["target"] == target
        and row["operation"] == operation
    )


# --- classifiers ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (200, None, "allow"),
        (204, None, "allow"),
        (403, "AccessDenied", "deny"),
        (403, "SignatureDoesNotMatch", "error"),
        (403, None, "error"),
        (404, "NoSuchKey", "missing"),
        (404, None, "missing"),
        (400, "InvalidRequest", "invalid"),
        (500, None, "error"),
        (0, None, "error"),
    ],
)
def test_classify_s3(status: int, code: str | None, expected: str) -> None:
    assert ps.classify_s3(status, code) == expected


@pytest.mark.parametrize(
    ("status", "code", "expected"),
    [
        (200, None, "allow"),
        (403, "AccessDenied", "deny"),
        (403, None, "deny"),
        (400, "InvalidParameterValue", "invalid"),
        (400, None, "invalid"),
        (500, None, "error"),
    ],
)
def test_classify_sts(status: int, code: str | None, expected: str) -> None:
    assert ps.classify_sts(status, code) == expected


def test_s3_call_reads_the_status_of_a_successful_call() -> None:
    class Client:
        def delete_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"ResponseMetadata": {"HTTPStatusCode": 204}}

    assert ps.s3_call(Client(), "delete_object", Bucket="b", Key="k") == (204, None)


def test_s3_call_maps_an_exception_without_an_error_block_to_403_and_no_code() -> None:
    class Client:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise FakeClientError(403)

    assert ps.s3_call(Client(), "get_object", Bucket="b", Key="k") == (403, None)


def test_s3_call_returns_the_error_code_of_a_denial() -> None:
    class Client:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise FakeClientError(403, "AccessDenied")

    assert ps.s3_call(Client(), "get_object", Bucket="b", Key="k") == (403, "AccessDenied")


def test_s3_call_lets_an_exception_without_a_response_propagate() -> None:
    class Client:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            raise ConnectionResetError("reset")

    with pytest.raises(ConnectionResetError):
        ps.s3_call(Client(), "get_object", Bucket="b", Key="k")


# --- the matrix and the key helpers --------------------------------------------------------------


def expectation(read_only: bool, principal: str, target: str, operation: str) -> str:
    found = [
        row[3] for row in ps.expected_matrix(read_only) if row[:3] == (principal, target, operation)
    ]
    assert len(found) == 1, (principal, target, operation)
    return found[0]


@pytest.mark.parametrize("operation", ["put", "delete", "multipart"])
def test_a_read_only_run_expects_own_writes_denied(operation: str) -> None:
    assert expectation(True, "vended", "own", operation) == "deny"
    assert expectation(False, "vended", "own", operation) == "allow"


@pytest.mark.parametrize("operation", ["get", "list"])
def test_own_reads_are_allowed_in_both_modes(operation: str) -> None:
    assert expectation(True, "vended", "own", operation) == "allow"
    assert expectation(False, "vended", "own", operation) == "allow"


@pytest.mark.parametrize("read_only", [True, False])
def test_both_modes_deny_the_lookalike_put_and_both_root_list_forms(read_only: bool) -> None:
    assert expectation(read_only, "vended", "lookalike", "put") == "deny"
    assert expectation(read_only, "vended", "root", "list_prefix_empty") == "deny"
    assert expectation(read_only, "vended", "root", "list_no_prefix") == "deny"


@pytest.mark.parametrize("read_only", [True, False])
def test_every_denial_has_a_positive_control_on_the_same_operation(read_only: bool) -> None:
    rows = ps.expected_matrix(read_only)
    denied = {row[1:3] for row in rows if row[0] == "vended" and row[3] == "deny"}
    controls = {row[1:3] for row in rows if row[0] == "lakekeeper"}
    assert {pair for pair in denied if pair[0] in {"sibling", "root"}} <= controls
    assert all(row[3] == "allow" for row in rows if row[0] == "lakekeeper")


def test_the_matrix_order_is_fixed_and_has_no_duplicates() -> None:
    first = ps.expected_matrix(False)
    assert first == ps.expected_matrix(False)
    assert len(set(first)) == len(first)


@pytest.mark.parametrize(
    ("location", "key"),
    [
        ("s3://warehouse/0190abcd", "0190abcd"),
        ("s3://warehouse/0190abcd/", "0190abcd"),
        ("s3://warehouse/0190abcd//", "0190abcd/"),
        ("s3://warehouse/0190abcd/metadata/00000.json", "0190abcd/metadata/00000.json"),
    ],
)
def test_table_key_strips_the_bucket_and_one_trailing_slash(location: str, key: str) -> None:
    assert ps.table_key(location) == key


@pytest.mark.parametrize("location", ["s3://other/x", "http://warehouse/x", "warehouse/x"])
def test_table_key_rejects_a_location_outside_the_bucket(location: str) -> None:
    with pytest.raises(ValueError, match="location"):
        ps.table_key(location)


def test_vended_config_is_the_first_storage_credentials_entry() -> None:
    document = {
        "storage-credentials": [{"prefix": "p", "config": {"a": "1"}}, {"config": {"b": "2"}}]
    }
    assert ps.vended_config(document) == {"a": "1"}


@pytest.mark.parametrize(
    "document", [{}, {"storage-credentials": []}, {"storage-credentials": "x"}]
)
def test_vended_config_is_none_without_storage_credentials(document: dict[str, Any]) -> None:
    assert ps.vended_config(document) is None


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({"s3.session-token-expires-at-ms": str(int((NOW + 3600) * 1000))}, 3600),
        ({"s3.session-token-expires-at-ms": int((NOW - 5) * 1000)}, -5),
        ({}, None),
        ({"s3.session-token-expires-at-ms": "soon"}, None),
    ],
)
def test_expires_in_s(config: dict[str, Any], expected: int | None) -> None:
    assert ps.expires_in_s(config, NOW) == expected


# --- verdict ---------------------------------------------------------------------------------------


def test_verdict_go_when_every_expectation_is_met() -> None:
    assert ps.verdict(good_result()) == ("go", ps.GO_BRANCH)
    assert ps.verdict(good_result(read_only=True)) == ("go", ps.GO_BRANCH)


def test_verdict_fallback_when_there_are_no_storage_credentials() -> None:
    result = good_result()
    result["vending"]["with_header"] = False
    assert ps.verdict(result) == ("fallback", ps.FAIL_BRANCH)


def test_verdict_fallback_when_an_own_prefix_put_is_denied() -> None:
    result = mutate_row(
        good_result(),
        "matrix",
        vended_row("own", "put"),
        status=403,
        error="AccessDenied",
        observed="deny",
    )
    assert ps.verdict(result) == ("fallback", ps.FAIL_BRANCH)


@pytest.mark.parametrize(
    ("target", "operation"),
    [
        ("sibling", "get"),
        ("sibling", "get_metadata"),
        ("sibling", "put"),
        ("sibling", "delete"),
        ("sibling", "list"),
        ("sibling", "multipart"),
        ("lookalike", "put"),
        ("root", "get"),
        ("root", "put"),
        ("root", "delete"),
        ("root", "list_prefix_empty"),
        ("root", "list_no_prefix"),
    ],
)
def test_one_allowed_sibling_or_root_operation_is_the_bucket_wide_fallback(
    target: str, operation: str
) -> None:
    result = mutate_row(
        good_result(),
        "matrix",
        vended_row(target, operation),
        status=200,
        error=None,
        observed="allow",
    )
    assert ps.verdict(result) == ("fallback", ps.WIDE_BRANCH)


@pytest.mark.parametrize(
    "change",
    [
        lambda r: mutate_row(
            r,
            "matrix",
            vended_row("sibling", "get"),
            status=404,
            error="NoSuchKey",
            observed="missing",
        ),
        lambda r: mutate_row(
            r, "matrix", vended_row("root", "delete"), status=400, error=None, observed="invalid"
        ),
        lambda r: mutate_row(
            r, "matrix", vended_row("sibling", "put"), status=403, error=None, observed="error"
        ),
        lambda r: mutate_row(
            r,
            "matrix",
            control_row("sibling", "get"),
            status=403,
            error="AccessDenied",
            observed="deny",
        ),
        lambda r: mutate_row(
            r,
            "matrix",
            control_row("root", "list_no_prefix"),
            status=404,
            error=None,
            observed="missing",
        ),
        lambda r: mutate_row(
            r,
            "assume_role",
            lambda row: row["principal"] == "lakekeeper",
            status=403,
            observed="deny",
        ),
        lambda r: mutate_row(
            r,
            "assume_role",
            lambda row: row["principal"] == "admin",
            status=400,
            error="InvalidParameterValue",
            observed="invalid",
        ),
    ],
)
def test_verdict_is_inconclusive_when_a_control_or_a_status_cannot_be_trusted(
    change: Callable[[dict[str, Any]], dict[str, Any]],
) -> None:
    verdict, _ = ps.verdict(change(good_result()))
    assert verdict == "inconclusive"


@pytest.mark.parametrize(
    "vending",
    [
        {"expires_in_s": 3661},
        {"expires_in_s": 0},
        {"expires_in_s": -20},
        {"expires_in_s": None},
        {"prefix_matches_location": False},
    ],
)
def test_verdict_is_inconclusive_when_the_vending_checks_fail(vending: dict[str, Any]) -> None:
    result = good_result()
    result["vending"].update(vending)
    assert ps.verdict(result)[0] == "inconclusive"


ABSENT = object()


@pytest.mark.parametrize(
    "vending",
    [
        {"without_header": True},
        {"without_header": False},
        {"without_header": None},
        {"without_header": ABSENT},
        {"without_header": True, "without_header_prefix_matches_location": True},
        {"without_header": True, "without_header_prefix_matches_location": False},
        {"without_header": True, "without_header_prefix_matches_location": None},
        {"without_header_prefix_matches_location": ABSENT, "without_header_config_vends": ABSENT},
        {"without_header": True, "without_header_config_vends": True},
        {"without_header": False, "without_header_config_vends": False},
    ],
)
def test_the_no_header_result_never_moves_the_verdict(vending: dict[str, Any]) -> None:
    result = good_result()
    for name, value in vending.items():
        if value is ABSENT:
            result["vending"].pop(name, None)
        else:
            result["vending"][name] = value
    assert ps.verdict(result) == ("go", ps.GO_BRANCH)


def test_verdict_is_inconclusive_without_any_positive_control() -> None:
    result = good_result()
    result["matrix"] = [row for row in result["matrix"] if row["principal"] != "lakekeeper"]
    assert ps.verdict(result)[0] == "inconclusive"


@pytest.mark.parametrize("principal", ["probe-other", "admin", "garbage"])
def test_verdict_is_trust_breach_when_another_identity_can_assume_the_role(principal: str) -> None:
    result = mutate_row(
        good_result(),
        "assume_role",
        lambda row: row["principal"] == principal,
        status=200,
        error=None,
        observed="allow",
    )
    assert ps.verdict(result)[0] == "trust-breach"


# --- queues -----------------------------------------------------------------------------------------


def test_the_verified_lakekeeper_payload_has_neither_maintenance_queue() -> None:
    assert ps.classify_queues(INFO, OPENAPI_PATHS) == {
        "expire_snapshots": "absent",
        "orphan_removal": "absent",
    }


@pytest.mark.parametrize(
    ("queue", "expected"),
    [
        ("expire_snapshots", {"expire_snapshots": "present", "orphan_removal": "absent"}),
        ("remove_orphan_files", {"expire_snapshots": "absent", "orphan_removal": "present"}),
        ("Orphan-Removal", {"expire_snapshots": "absent", "orphan_removal": "present"}),
        ("snapshot_expiry_and_expire", {"expire_snapshots": "present", "orphan_removal": "absent"}),
        ("tabular_expiration", {"expire_snapshots": "absent", "orphan_removal": "absent"}),
        ("tabular_purge", {"expire_snapshots": "absent", "orphan_removal": "absent"}),
        ("snapshot_cleanup", {"expire_snapshots": "absent", "orphan_removal": "absent"}),
        ("expire_tokens", {"expire_snapshots": "absent", "orphan_removal": "absent"}),
    ],
)
def test_a_queue_counts_only_when_its_name_holds_the_right_words(
    queue: str, expected: dict[str, str]
) -> None:
    assert ps.classify_queues({"queues": [queue]}, []) == expected


@pytest.mark.parametrize("info", [{}, {"queues": []}, {"queues": None}, {"queues": "x"}])
def test_an_empty_or_missing_queue_list_is_inconclusive(info: dict[str, Any]) -> None:
    assert ps.classify_queues(info, OPENAPI_PATHS) == {
        "expire_snapshots": "inconclusive",
        "orphan_removal": "inconclusive",
    }


def test_the_queue_classification_ignores_the_order_of_the_lists() -> None:
    forward = ps.classify_queues(INFO, OPENAPI_PATHS)
    reversed_info = {**INFO, "queues": list(reversed(INFO["queues"]))}
    assert ps.classify_queues(reversed_info, list(reversed(OPENAPI_PATHS))) == forward
    extra = {**INFO, "queues": ["z", "expire_snapshots", *INFO["queues"]]}
    assert ps.classify_queues(extra, OPENAPI_PATHS) == ps.classify_queues(
        {**extra, "queues": sorted(extra["queues"])}, reversed(OPENAPI_PATHS)
    )


def test_a_queue_that_only_the_openapi_paths_name_still_counts() -> None:
    paths = [
        *OPENAPI_PATHS,
        "/management/v1/warehouse/{warehouse_id}/task-queue/expire_snapshots/config",
    ]
    assert ps.classify_queues(INFO, paths)["expire_snapshots"] == "present"


# --- the no-header observation -----------------------------------------------------------------------

LOCATION = "s3://warehouse/0190aaaa"


def key_fields(**changes: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "s3.access-key-id": secrets.token_hex(8),
        "s3.secret-access-key": secrets.token_hex(16),
        "s3.session-token": secrets.token_hex(24),
    }
    fields.update(changes)
    return fields


def no_header_response(prefix: object, config: object) -> dict[str, Any]:
    entry = {"prefix": prefix, "config": key_fields()}
    return {"storage-credentials": [entry], "config": config}


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ({"config": {}}, (False, None, False)),
        ({}, (False, None, False)),
        ({"storage-credentials": []}, (False, None, False)),
        (no_header_response(LOCATION, {}), (True, True, False)),
        (no_header_response(LOCATION + "/", {}), (True, True, False)),
        (no_header_response(LOCATION + "//", {}), (True, False, False)),
        (no_header_response("s3://warehouse", {}), (True, False, False)),
        (no_header_response("s3://warehouse/0190aaaax", {}), (True, False, False)),
        (no_header_response(None, {}), (True, False, False)),
        (no_header_response(LOCATION, key_fields()), (True, True, True)),
        (no_header_response(LOCATION, key_fields(**{"s3.session-token": ""})), (True, True, False)),
        (
            no_header_response(LOCATION, key_fields(**{"s3.secret-access-key": 7})),
            (True, True, False),
        ),
        (no_header_response(LOCATION, "not a mapping"), (True, True, False)),
        (
            {
                "storage-credentials": [{"prefix": LOCATION}],
                "config": key_fields(),
            },
            (False, True, True),
        ),
    ],
)
def test_no_header_observation(
    response: dict[str, Any], expected: tuple[bool, bool | None, bool]
) -> None:
    observed = ps.no_header_observation(response, LOCATION)
    assert list(observed) == [
        "without_header",
        "without_header_prefix_matches_location",
        "without_header_config_vends",
    ]
    assert tuple(observed.values()) == expected


def test_no_header_observation_without_a_config_vends_only_when_all_three_fields_are_there() -> (
    None
):
    config = key_fields()
    del config["s3.access-key-id"]
    observed = ps.no_header_observation(no_header_response(LOCATION, config), LOCATION)
    assert observed["without_header_config_vends"] is False


def test_the_no_header_field_names_are_not_credential_names() -> None:
    for name in (
        "without_header",
        "without_header_prefix_matches_location",
        "without_header_config_vends",
    ):
        assert not redact_evidence.is_secret_name(name)


# --- main end to end ----------------------------------------------------------------------------------


def test_main_prints_one_json_line_with_the_go_verdict_and_leaks_no_secret(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = run_main(capsys, "--json")
    assert code == 0
    assert len(out.strip().splitlines()) == 1
    document = json.loads(out)
    assert (document["verdict"], document["branch"]) == ("go", ps.GO_BRANCH)
    assert list(document) == [
        "verdict",
        "branch",
        "read_only",
        "vending",
        "matrix",
        "assume_role",
        "queues",
        "observations",
    ]
    assert all(row["ok"] for row in document["matrix"] + document["assume_role"])
    assert document["queues"]["expire_snapshots"] == "absent"
    assert document["queues"]["orphan_removal"] == "absent"
    assert document["queues"]["any_name_route_status"] == 200
    assert document["queues"]["delete_profile"] == "hard"
    assert document["vending"] == {
        "with_header": True,
        "without_header": False,
        "without_header_prefix_matches_location": None,
        "without_header_config_vends": False,
        "expires_in_s": 3600,
        "prefix_matches_location": True,
    }
    assert list(document["vending"]) == [
        "with_header",
        "without_header",
        "without_header_prefix_matches_location",
        "without_header_config_vends",
        "expires_in_s",
        "prefix_matches_location",
    ]
    assert world.handed_out
    for secret in world.handed_out:
        assert secret not in out
        assert secret not in err
    assert not [key for key in keys_of(document) if redact_evidence.is_secret_name(key)]


def test_main_is_go_when_lakekeeper_vends_without_the_delegation_header(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    fake = World(vends_without_header=True)
    install(monkeypatch, fake)
    code, out, err = run_main(capsys, "--json")
    assert code == 0
    document = json.loads(out)
    assert (document["verdict"], document["branch"]) == ("go", ps.GO_BRANCH)
    assert document["vending"] == {
        "with_header": True,
        "without_header": True,
        "without_header_prefix_matches_location": True,
        "without_header_config_vends": True,
        "expires_in_s": 3600,
        "prefix_matches_location": True,
    }
    assert all(row["ok"] for row in document["matrix"] + document["assume_role"])
    assert fake.handed_out
    for secret in fake.handed_out:
        assert secret not in out
        assert secret not in err
    assert not [key for key in keys_of(document) if redact_evidence.is_secret_name(key)]


def test_a_no_header_entry_for_another_prefix_is_recorded_and_still_go(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, World(vends_without_header=True, no_header_prefix="s3://warehouse"))
    code, out, _ = run_main(capsys, "--json")
    document = json.loads(out)
    assert code == 0
    assert (document["verdict"], document["branch"]) == ("go", ps.GO_BRANCH)
    assert document["vending"]["without_header"] is True
    assert document["vending"]["without_header_prefix_matches_location"] is False


def test_the_garbage_key_is_built_at_run_time_and_is_not_one_of_the_real_keys(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(capsys, "--json")
    real = {*world.control_pair, *world.other_pair, *world.admin_pair, *world.vended_pair}
    garbage = [key for key in world.sts_log if key not in real]
    assert len(garbage) == 1
    assert len(world.sts_log) == 4


def test_the_look_alike_prefix_and_both_root_lists_are_attempted_with_the_vended_key(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(capsys, "--json")
    own = UUIDS["a"]
    attempts = [(op, key) for who, op, key in world.s3_log if who == "vended"]
    assert ("put_object", f"{own}x/probe.bin") in attempts
    assert ("list_objects_v2", "") in attempts
    assert ("list_objects_v2", "<none>") in attempts
    assert ("list_objects_v2", f"{own}/") in attempts
    assert ("create_multipart_upload", f"{UUIDS['b']}/probe-multipart.bin") in attempts


def test_the_multipart_upload_is_twelve_mib_in_parts_of_at_least_five(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    assert ps.MULTIPART_PART_BYTES == 5 * 2**20
    run_main(capsys, "--json")
    parts = [entry for entry in world.s3_log if entry[1] == "upload_part" and entry[0] == "vended"]
    assert len(parts) == 3


def test_the_list_without_the_slash_and_list_buckets_are_observations_not_scored(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _, out, _ = run_main(capsys, "--json")
    document = json.loads(out)
    operations = {row["operation"] for row in document["matrix"]}
    assert "list_no_slash" not in operations
    assert "list_buckets" not in operations
    observed = {row["operation"]: row["observed"] for row in document["observations"]}
    assert observed == {"list_no_slash": "deny", "list_buckets": "allow"}


def test_the_matrix_rows_follow_the_fixed_order_and_a_second_run_prints_the_same_line(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    _, first, _ = run_main(capsys, "--json")
    _, second, _ = run_main(capsys, "--json")
    assert first == second
    rows = json.loads(first)["matrix"]
    assert [(r["principal"], r["target"], r["operation"]) for r in rows] == [
        row[:3] for row in ps.expected_matrix(False)
    ]


def test_a_read_only_run_against_a_read_only_credential_is_still_go(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, World(vended_read_only=True))
    code, out, _ = run_main(capsys, "--json", "--read-only")
    document = json.loads(out)
    assert (code, document["verdict"], document["read_only"]) == (0, "go", True)
    own_put = next(
        r for r in document["matrix"] if r["target"] == "own" and r["operation"] == "put"
    )
    assert (own_put["expected"], own_put["observed"]) == ("deny", "deny")


def test_a_credential_that_reaches_the_whole_bucket_is_the_bucket_wide_fallback(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    install(monkeypatch, World(vended_reaches_all=True))
    code, out, _ = run_main(capsys, "--json")
    document = json.loads(out)
    assert code == 1
    assert (document["verdict"], document["branch"]) == ("fallback", ps.WIDE_BRANCH)


def test_without_json_main_prints_one_line_per_check(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = run_main(capsys)
    lines = [line for line in out.splitlines() if line.strip()]
    expected = len(ps.expected_matrix(False)) + 4
    checks = [line for line in lines if " expected=" in line and " ok=" in line]
    assert code == 0
    assert len(checks) == expected
    first = checks[0]
    for field in ("vended", "own", "put", "status=", "error=", "expected=allow", "ok=True"):
        assert field in first
    assert not [line for line in lines if line.startswith("{")]
    for secret in world.handed_out:
        assert secret not in out


@pytest.mark.parametrize("name", AMBIENT)
def test_main_refuses_an_ambient_aws_variable_naming_it_but_not_its_value(
    name: str, world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    value = secrets.token_hex(12)
    monkeypatch.setenv(name, value)
    code, out, err = run_main(capsys, "--json")
    assert code == 2
    assert name in err
    assert value not in out + err
    assert world.http_log == []


def test_main_names_a_missing_variable(
    world: World, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("LAKEKEEPER_S3_KEY")
    code, _, err = run_main(capsys, "--json")
    assert code == 2
    assert "missing environment variable: LAKEKEEPER_S3_KEY" in err
    assert world.http_log == []


def test_main_reports_an_os_error_by_type_name_only(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fail_on = "/management/v1/info"
    code, out, err = run_main(capsys, "--json")
    assert code == 1
    assert "OSError" in err
    assert "boom" not in out + err


# --- cleanup and idempotency ----------------------------------------------------------------------------


def test_leftovers_of_an_interrupted_run_are_dropped_before_the_new_tables_are_created(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.leave_leftovers()
    code, out, _ = run_main(capsys, "--json")
    assert code == 0
    assert json.loads(out)["verdict"] == "go"
    drops = [i for i, (method, path) in enumerate(world.http_log) if method == "DELETE"]
    creates = [i for i, (method, path) in enumerate(world.http_log) if method == "POST"]
    assert drops
    assert max(drops[:3]) < min(creates)


def test_cleanup_drops_the_tables_and_the_namespace_and_the_probe_keys(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    run_main(capsys, "--json")
    assert world.tables == {}
    assert world.namespace is False
    assert ps.ROOT_MARKER not in world.objects
    assert not [key for key in world.objects if key.endswith(("probe.bin", "probe-seed.bin"))]


def test_cleanup_still_runs_when_a_leg_raises(
    world: World, capsys: pytest.CaptureFixture[str]
) -> None:
    world.fail_on = "/management/v1/info"
    code, _, _ = run_main(capsys, "--json")
    assert code == 1
    assert world.tables == {}
    assert world.namespace is False
    assert ps.ROOT_MARKER not in world.objects
