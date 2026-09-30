"""Unit tests for the evidence redactor.

Every fake secret is generated at runtime, so no secret-shaped literal enters git history.
"""

from __future__ import annotations

import base64
import io
import secrets
from pathlib import Path

import pytest
import redact_evidence as re_
from dotenv_lite import DEFAULT_ENV_FILE, REPO_ROOT

MASK = re_.REDACTED


def hexval() -> str:
    return secrets.token_hex(12)


def jwt_like() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    return f"{header}.{secrets.token_urlsafe(18)}.{secrets.token_urlsafe(18)}"


@pytest.mark.parametrize(
    "template",
    [
        '{"password": "%s"}',
        "password: %s",
        "  password: %s",
        "password=%s",
        "database.password=%s",
        "{'password': '%s'}",
        "'password' => '%s'",
        "AWS_SECRET_ACCESS_KEY=%s",
        "dbt_token: %s",
        "api_key = %s",
        "DBT_CLOUD_TOKEN=%s",
        "client_secret: %s",
        "passwd=%s",
        "gcp_credentials: %s",
    ],
)
def test_layer1_masks_the_value_and_keeps_the_structure(template: str) -> None:
    value = hexval()
    out = re_.redact_shapes(template % value)
    assert value not in out
    assert out == template % MASK


def test_vended_credential_json_is_masked() -> None:
    a, b, c = hexval(), hexval(), hexval()
    text = (
        "{"
        f'"s3.access-key-id": "{a}", '
        f'"s3.secret-access-key": "{b}", '
        f'"s3.session-token": "{c}", '
        '"s3.endpoint": "http://seaweedfs:8333"'
        "}"
    )
    out = re_.redact_shapes(text)
    for value in (a, b, c):
        assert value not in out
    assert '"s3.access-key-id": "REDACTED"' in out
    assert '"s3.session-token": "REDACTED"' in out
    assert '"s3.endpoint": "http://seaweedfs:8333"' in out


def test_properties_style_credentials_are_masked() -> None:
    a = hexval()
    out = re_.redact_shapes(f"s3.access-key-id={a}\ns3.path-style-access=true\n")
    assert out == "s3.access-key-id=REDACTED\ns3.path-style-access=true\n"


def test_quoted_value_with_spaces_is_masked_whole() -> None:
    out = re_.redact_shapes('password = "two words here"')
    assert out == 'password = "REDACTED"'


def test_empty_value_does_not_swallow_the_next_line() -> None:
    text = "password:\nregion: local\n"
    assert re_.redact_shapes(text) == text


def test_dsn_password_is_masked() -> None:
    value = hexval()
    out = re_.redact_shapes(f"postgresql://lakekeeper:{value}@postgres:5432/lakekeeper")
    assert out == "postgresql://lakekeeper:REDACTED@postgres:5432/lakekeeper"


def test_sts_xml_secret_elements_are_masked() -> None:
    a, b, c = hexval(), hexval(), jwt_like()
    text = (
        "<Credentials>\n"
        f"  <AccessKeyId>{a}</AccessKeyId>\n"
        f"  <SecretAccessKey>{b}</SecretAccessKey>\n"
        f"  <SessionToken>{c}</SessionToken>\n"
        "  <Expiration>2026-09-30T12:00:00Z</Expiration>\n"
        "</Credentials>\n"
    )
    out = re_.redact_shapes(text)
    for value in (a, b, c):
        assert value not in out
    assert "<AccessKeyId>REDACTED</AccessKeyId>" in out
    assert "<SecretAccessKey>REDACTED</SecretAccessKey>" in out
    assert "<SessionToken>REDACTED</SessionToken>" in out
    assert "<Expiration>2026-09-30T12:00:00Z</Expiration>" in out


@pytest.mark.parametrize(
    "header",
    ["Authorization", "authorization", "X-Amz-Security-Token", "X-Amz-Signature", "X-Api-Key"],
)
def test_auth_headers_are_masked(header: str) -> None:
    value = hexval()
    out = re_.redact_shapes(f"{header}: Bearer {value}\nContent-Type: text/xml\n")
    assert value not in out
    assert out == f"{header}: REDACTED\nContent-Type: text/xml\n"


def test_jwt_shaped_string_is_masked() -> None:
    value = jwt_like()
    out = re_.redact_shapes(f"got {value} back")
    assert out == "got REDACTED back"


@pytest.mark.parametrize(
    "line",
    [
        "key: 42",
        "primary_key: id",
        "partition_key: 7",
        "message_key: abc",
        "sort-key: x",
        "foreignKey: order_id",
        "tokenDuration: 1h",
        "sts-token-validity-seconds: 3600",
        "token_count: 5",
        "Fingerprint: planted-secret.env:shopstream-token:1",
    ],
)
def test_excluded_names_are_left_alone(line: str) -> None:
    assert re_.redact_shapes(line) == line


def write_env(tmp_path: Path, body: str) -> Path:
    path = tmp_path / ".env"
    path.write_text(body, encoding="utf-8")
    return path


def test_layer2_masks_env_literals_wherever_they_appear() -> None:
    value = hexval()
    out = re_.redact_literals(f"the run used {value} twice: {value}", [value])
    assert out == "the run used REDACTED twice: REDACTED"


def test_layer2_masks_the_longer_of_two_overlapping_values_first() -> None:
    inner = "inner" + secrets.token_hex(6)
    outer = "pre" + inner + "post"
    text = f"a={outer} b={inner}"
    for order in ([inner, outer], [outer, inner]):
        out = re_.redact_literals(text, order)
        assert out == "a=REDACTED b=REDACTED"
        assert "pre" not in out
        assert "post" not in out


def test_layer2_ignores_values_shorter_than_the_minimum() -> None:
    short = "x" * (re_.MIN_LITERAL_LENGTH - 1)
    text = f"value {short} stays"
    assert re_.redact_literals(text, [short]) == text
    exact = "y" * re_.MIN_LITERAL_LENGTH
    assert re_.redact_literals(f"v {exact}", [exact]) == "v REDACTED"


def test_paths_repo_root_is_replaced_before_home(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    repo = home / "Code" / "shopstream"
    text = f"cd {repo}/infra && ls {home}/.docker; also /host_mnt{home}/x"
    out = re_.redact_paths(text, repo, home)
    assert out == "cd <repo>/infra && ls <home>/.docker; also /host_mnt<home>/x"


def test_paths_accept_several_home_directories(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    first, second = tmp_path / "h1", tmp_path / "h2"
    out = re_.redact_paths(f"{first}/a {second}/b", repo, [first, second])
    assert out == "<home>/a <home>/b"


def test_redact_composes_the_layers_in_order(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    repo = home / "Code" / "shopstream"
    literal = "lit" + secrets.token_hex(8)
    dsn_value = hexval()
    text = (
        f"password: {hexval()}\n"
        f"postgresql://u:{dsn_value}@db:5432/x\n"
        f"note {literal} seen in {repo}/infra/.env and {home}/.config\n"
    )
    out = re_.redact(text, [literal], repo, home)
    assert dsn_value not in out
    assert literal not in out
    assert str(home) not in out
    assert "password: REDACTED" in out
    assert "note REDACTED seen in <repo>/infra/.env and <home>/.config" in out


def test_secret_containing_a_path_fragment_is_masked_whole(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    repo = home / "Code" / "shopstream"
    literal = f"{home}/keys/abc123xyz"
    out = re_.redact(f"path is {literal}", [literal], repo, home)
    assert out == "path is REDACTED"


def test_redaction_is_idempotent(tmp_path: Path) -> None:
    home = tmp_path / "home" / "someone"
    repo = home / "Code" / "shopstream"
    literal = "lit" + secrets.token_hex(8)
    text = (
        f'{{"s3.session-token": "{hexval()}"}}\n'
        f"<SecretAccessKey>{hexval()}</SecretAccessKey>\n"
        f"Authorization: Bearer {hexval()}\n"
        f"jwt {jwt_like()}\n"
        f"postgresql://u:{hexval()}@db/x\n"
        f"{literal} in {repo}/a and {home}/b\n"
    )
    once = re_.redact(text, [literal], repo, home)
    assert re_.redact(once, [literal], repo, home) == once


def run_main(monkeypatch: pytest.MonkeyPatch, stdin: str, argv: list[str]) -> tuple[int, str, str]:
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin))
    out, err = io.StringIO(), io.StringIO()
    monkeypatch.setattr("sys.stdout", out)
    monkeypatch.setattr("sys.stderr", err)
    code = re_.main(argv)
    return code, out.getvalue(), err.getvalue()


def test_main_empty_stdin_gives_empty_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    code, out, _ = run_main(monkeypatch, "", ["--env-file", str(tmp_path / "missing.env")])
    assert (code, out) == (0, "")


def test_main_without_an_env_file_still_runs_layer1_and_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    literal = "lit" + secrets.token_hex(8)
    text = f"password: {hexval()}\nkept {literal}\ncwd {REPO_ROOT}/infra\n"
    code, out, _ = run_main(monkeypatch, text, ["--env-file", str(tmp_path / "missing.env")])
    assert code == 0
    assert "password: REDACTED" in out
    assert f"kept {literal}" in out
    assert "cwd <repo>/infra" in out


def test_main_uses_env_file_values_for_layer2(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    literal = "lit" + secrets.token_hex(8)
    env = write_env(tmp_path, f"# comment\nSOME_VALUE={literal}\nSHORT=abc\n")
    code, out, _ = run_main(monkeypatch, f"seen {literal} and abc\n", ["--env-file", str(env)])
    assert code == 0
    assert out == "seen REDACTED and abc\n"


def test_main_malformed_env_file_exits_2_without_printing_values(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    leaked = "lit" + secrets.token_hex(8)
    env = write_env(tmp_path, f"GOOD=fine-value\n{leaked}\n")
    code, out, err = run_main(monkeypatch, "anything\n", ["--env-file", str(env)])
    assert code == 2
    assert out == ""
    assert leaked not in err
    assert "line 2" in err


def test_default_env_file_is_infra_dot_env() -> None:
    assert DEFAULT_ENV_FILE == REPO_ROOT / "infra" / ".env"
