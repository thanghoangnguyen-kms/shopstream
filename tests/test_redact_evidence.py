"""Unit tests for the evidence redactor.

Every fake secret is generated at runtime, so no secret-shaped literal enters git history.
"""

from __future__ import annotations

import base64
import io
import secrets
import string
from pathlib import Path

import pytest
import redact_evidence as re_
from dotenv_lite import DEFAULT_ENV_FILE, REPO_ROOT

MASK = re_.REDACTED


def hexval() -> str:
    return secrets.token_hex(12)


class Fresh(dict[str, str]):
    """A mapping that invents a new runtime secret for every placeholder it is asked about."""

    def __missing__(self, key: str) -> str:
        self[key] = secrets.token_hex(12)
        return self[key]


def fill(template: str) -> tuple[str, list[str]]:
    """Fill each `${name}` in a template with a fresh runtime value; return text and values."""
    values = Fresh()
    text = string.Template(template).substitute(values)
    return text, list(values.values())


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


# (id, raw template, expected output) for each leak reproduced in 01-REVIEW.md CR-01 and WR-02.
# `${name}` placeholders are filled at run time, so no secret-shaped literal is committed.
NESTED_CASES: list[tuple[str, str, str]] = [
    ("query-string", "http://h/x?user=bob&password=${v}", "http://h/x?user=bob&password=REDACTED"),
    ("semicolon", "a=1;password=${v}", "a=1;password=REDACTED"),
    (
        "logfmt-quoted-msg",
        'level=error msg="connect failed password=${v} host=x"',
        'level=error msg="connect failed password=REDACTED host=x"',
    ),
    ("flag-equals", "postgres --password=${v}", "postgres --password=REDACTED"),
    (
        "yaml-command",
        'command: "postgres --password=${v}"',
        'command: "postgres --password=REDACTED"',
    ),
    (
        "presigned-url",
        "https://b.s3/x?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=${id}/20260930/us-east-1/s3/aws4_request"
        "&X-Amz-Date=20260930T000000Z&X-Amz-SignedHeaders=host&X-Amz-Signature=${v}",
        "https://b.s3/x?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        "&X-Amz-Credential=REDACTED"
        "&X-Amz-Date=20260930T000000Z&X-Amz-SignedHeaders=host&X-Amz-Signature=REDACTED",
    ),
    (
        "bare-signature",
        "request failed Signature=${v} for key x",
        "request failed Signature=REDACTED for key x",
    ),
    ("yaml-spaces", "password: ${w1} ${w2} ${w3} ${w4}", "password: REDACTED"),
    ("mid-line-colon", "Error: password: ${w1} ${w2}", "Error: password: REDACTED"),
    ("pgpassword-comma", "PGPASSWORD=${a},${b}", "PGPASSWORD=REDACTED"),
    ("compose-list", "  - POSTGRES_PASSWORD=${v}", "  - POSTGRES_PASSWORD=REDACTED"),
    ("export", "export TOKEN=${a} ${b}", "export TOKEN=REDACTED"),
    ("escaped-quote", '{"password": "ab\\"cd${v}"}', '{"password": "REDACTED"}'),
    ("unterminated-quote", 'password: "unclosed ${v}', 'password: "REDACTED'),
    (
        "quoted-msg-colon",
        'msg="bad password: ${v}" host=y',
        'msg="bad password: REDACTED" host=y',
    ),
    ("amp-adjacent", "password=${a}&token=${b}", "password=REDACTED&token=REDACTED"),
    ("semicolon-adjacent", "password=${a};api_key=${b}", "password=REDACTED;api_key=REDACTED"),
]


@pytest.mark.parametrize(
    ("template", "expected"),
    [pytest.param(template, expected, id=name) for name, template, expected in NESTED_CASES],
)
def test_nested_and_whole_value_credentials_are_masked(template: str, expected: str) -> None:
    raw, values = fill(template)
    out = re_.redact_shapes(raw)
    assert out == expected
    for value in values:
        assert value not in out


@pytest.mark.parametrize(
    "line",
    [
        "SignedHeaders=host;x-amz-date",
        "X-Amz-Date=20260930T000000Z",
        "unique_key: order_id",
        "signature_version: s3v4",
        "sts:AssumeRole",
        "arn:aws:s3:::warehouse/*",
        "user=bob&region=local&host=db",
    ],
)
def test_nested_pairs_leave_non_secret_values_alone(line: str) -> None:
    assert re_.redact_shapes(line) == line


@pytest.mark.parametrize(
    "text",
    [
        "password=&user=bob",
        'password: ""',
        "token=",
        "password:   \nregion: local\n",
        "password: REDACTED   ",
        'password: "REDACTED"  ',
    ],
)
def test_empty_values_consume_nothing(text: str) -> None:
    assert re_.redact_shapes(text) == text


# (id, raw template, expected output) for the header, bearer, flag and DSN leaks in WR-01, WR-02.
SHAPE_CASES: list[tuple[str, str, str]] = [
    (
        "curl-single-quoted",
        "curl -H 'Authorization: Bearer ${v}' http://x",
        "curl -H 'Authorization: REDACTED' http://x",
    ),
    (
        "curl-double-quoted",
        'curl -H "Authorization: Bearer ${v}"',
        'curl -H "Authorization: REDACTED"',
    ),
    (
        "curl-verbose-sigv4",
        "> Authorization: AWS4-HMAC-SHA256 Credential=${id}/20260930/us-east-1/s3/aws4_request,"
        " SignedHeaders=host;x-amz-date, Signature=${v}",
        "> Authorization: REDACTED",
    ),
    ("set-cookie", "< Set-Cookie: sid=${v}; Path=/; HttpOnly", "< Set-Cookie: REDACTED"),
    ("cookie", "Cookie: session=${v}; theme=dark", "Cookie: REDACTED"),
    ("proxy-authorization", "Proxy-Authorization: Basic ${v}", "Proxy-Authorization: REDACTED"),
    ("bare-bearer", "using Bearer ${v} for the call", "using Bearer REDACTED for the call"),
    ("flag-space", "postgres --password ${v} -h db", "postgres --password REDACTED -h db"),
    ("flag-quoted", 'cli --token "${a} ${b}"', 'cli --token "REDACTED"'),
    (
        "dsn-at-in-password",
        "postgresql://u:${a}@${b}@host:5432/db",
        "postgresql://u:REDACTED@host:5432/db",
    ),
    ("dsn-empty-user", "redis://:${v}@cache:6379", "redis://:REDACTED@cache:6379"),
]

SHAPE_ORDER = [
    "PEM private key",
    "auth header",
    "bearer token",
    "credential-named field",
    "secret CLI flag",
    "DSN password",
    "XML credential element",
    "JWT-shaped string",
]

PEM_LABELS = [
    "PRIVATE KEY",
    "RSA PRIVATE KEY",
    "EC PRIVATE KEY",
    "OPENSSH PRIVATE KEY",
    "ENCRYPTED PRIVATE KEY",
]


def pem_line(kind: str, label: str) -> str:
    """A PEM marker line, built from parts so no full private-key marker pair sits in the source."""
    return "-----" + kind + " " + label + "-----"


def pem_body() -> list[str]:
    """Two base64 lines from 48 random bytes (64 characters, so no `=` padding)."""
    encoded = base64.b64encode(secrets.token_bytes(48)).decode()
    return [encoded[:32], encoded[32:]]


@pytest.mark.parametrize(
    ("template", "expected"),
    [pytest.param(template, expected, id=name) for name, template, expected in SHAPE_CASES],
)
def test_header_bearer_flag_and_dsn_shapes_are_masked(template: str, expected: str) -> None:
    raw, values = fill(template)
    out = re_.redact_shapes(raw)
    assert out == expected
    for value in values:
        assert value not in out


@pytest.mark.parametrize(
    "line",
    [
        "x-authorization-mode: none",
        "authorization_endpoint: http://127.0.0.1:8181/x",
        "Content-Type: text/xml",
        "a bearer of news",
        "Bearer short",
        "docker login --password-stdin",
        "psql --no-password -h db",
        "pg_dump --host db",
        "http://host:8080/x",
        "Authorization:",
        "Set-Cookie:   ",
        "postgres --password",
        "postgres --password\nnext line",
    ],
)
def test_non_secret_headers_and_flags_are_left_alone(line: str) -> None:
    assert re_.redact_shapes(line) == line


@pytest.mark.parametrize("label", PEM_LABELS)
def test_pem_private_key_bodies_are_masked_line_by_line(label: str) -> None:
    body = pem_body()
    begin, end = pem_line("BEGIN", label), pem_line("END", label)
    raw = "\n".join(["before", begin, *body, end, "after"]) + "\n"
    out = re_.redact_shapes(raw)
    assert out == "\n".join(["before", begin, MASK, MASK, end, "after"]) + "\n"
    assert not any(line in out for line in body)


def test_a_pem_block_cut_off_before_its_end_line_is_masked_to_the_end() -> None:
    body = pem_body()
    begin = pem_line("BEGIN", "RSA PRIVATE KEY")
    raw = "\n".join([begin, body[0], "", body[1]]) + "\n"
    out = re_.redact_shapes(raw)
    assert out == f"{begin}\n{MASK}\n\n{MASK}\n"


@pytest.mark.parametrize(
    "block",
    [
        pytest.param(
            "-----BEGIN CERTIFICATE-----\n"
            + "\n".join(pem_body())
            + "\n-----END CERTIFICATE-----\n",
            id="certificate",
        ),
        pytest.param(
            pem_line("BEGIN", "PRIVATE KEY") + "\n" + pem_line("END", "PRIVATE KEY") + "\n",
            id="empty-body",
        ),
    ],
)
def test_certificate_blocks_are_left_alone(block: str) -> None:
    assert re_.redact_shapes(block) == block


def test_multi_line_xml_elements_keep_their_lines() -> None:
    raw, values = fill("<SessionToken>\n${v}\n</SessionToken>\n<Token>  \n</Token>\n")
    out = re_.redact_shapes(raw)
    assert out == "<SessionToken>\nREDACTED\n</SessionToken>\n<Token>  \n</Token>\n"
    assert all(value not in out for value in values)
    assert out.count("\n") == raw.count("\n")


def test_shapes_run_in_the_documented_order() -> None:
    assert [reason for reason, _ in re_.SHAPES] == SHAPE_ORDER
