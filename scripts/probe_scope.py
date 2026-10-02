"""Item 1 credential-scope probe (skeleton: signatures only, filled in by the GREEN commit)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

ROLE_ARN = "arn:aws:iam::000000000000:role/LakekeeperVendedRole"
ROOT_MARKER = "probe-scope-root-marker"
MULTIPART_PART_BYTES = 5 * 2**20
GO_BRANCH = "All engines: vended credentials"
WIDE_BRANCH = "Vended but bucket-wide"
FAIL_BRANCH = "STS fails"


def http_request(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    raise NotImplementedError


def make_s3_client(
    access_key_id: str, secret_access_key: str, session_token: str | None = None
) -> object:
    raise NotImplementedError


def make_sts_client(access_key_id: str, secret_access_key: str) -> object:
    raise NotImplementedError


def now() -> float:
    raise NotImplementedError


def s3_call(client: object, operation: str, **kwargs: Any) -> tuple[int, str | None]:
    raise NotImplementedError


def classify_s3(status: int, code: str | None) -> str:
    raise NotImplementedError


def classify_sts(status: int, code: str | None = None) -> str:
    raise NotImplementedError


def expected_matrix(read_only: bool) -> tuple[tuple[str, str, str, str], ...]:
    raise NotImplementedError


def table_key(location: str) -> str:
    raise NotImplementedError


def vended_config(load_table_json: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raise NotImplementedError


def expires_in_s(config: Mapping[str, Any], at: float) -> int | None:
    raise NotImplementedError


def classify_queues(info: Mapping[str, Any], openapi_paths: Iterable[str]) -> dict[str, str]:
    raise NotImplementedError


def verdict(result: Mapping[str, Any]) -> tuple[str, str]:
    raise NotImplementedError


def run_probe(env: Mapping[str, str], read_only: bool) -> dict[str, Any]:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    raise NotImplementedError
