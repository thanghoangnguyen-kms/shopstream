"""The evidence gate: docs/evidence/**/*.md must hold no unmasked credential or PII.

`evidence_violations` reads every Markdown file under docs/evidence as strict UTF-8 and
reports each line that holds a credential or PII. Credential detection is the redactor's own
SHAPES rule, reached through `redact_evidence.shape_findings`: the gate flags exactly the lines
the redactor would still mask, so the two cannot drift apart. Email addresses and phone numbers
are gate-only checks. It is the CI backstop behind `scripts/redact_evidence.py`: the redactor
masks at capture time, this test proves nothing got through, including on a machine that has no
infra/.env. Fake secrets are generated at runtime so none enters git history.
"""

from __future__ import annotations

import base64
import json
import re
import secrets
from pathlib import Path

import dotenv_lite
import pytest
import redact_evidence

REPO = Path(__file__).resolve().parents[1]

EMAIL = re.compile(
    r"(?<![\w.+-])(?P<local>[A-Za-z0-9._%+-]+)@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)
PHONE_INTL = re.compile(r"(?<![\w+])\+\d{1,3}[ .-]?\(?\d{1,4}\)?(?:[ .-]?\d{2,4}){2,4}\b")
PHONE_NANP = re.compile(r"(?<![\d.-])\(?\d{3}\)?[ .-]\d{3}[ .-]\d{4}(?![\d.-])")

MASK = redact_evidence.REDACTED


def pii_violations(line: str) -> list[str]:
    """Reasons a single line holds an email address or a phone number."""
    reasons: list[str] = []
    # A DSN whose password is already masked reads like `REDACTED@host.tld`; not an email.
    if any(match.group("local") != MASK for match in EMAIL.finditer(line)):
        reasons.append("email address")
    if PHONE_INTL.search(line) or PHONE_NANP.search(line):
        reasons.append("phone number")
    return reasons


def line_violations(line: str) -> list[str]:
    """Reasons a single evidence line is not safe to publish."""
    reasons = [f"{reason} is not REDACTED" for _, reason in redact_evidence.shape_findings(line)]
    return reasons + pii_violations(line)


def evidence_violations(root: Path) -> list[str]:
    """Every violation under root/docs/evidence, as `<path>:<line>: <reason>`, sorted."""
    violations: list[tuple[str, int, str]] = []
    for path in sorted((root / "docs" / "evidence").glob("**/*.md")):
        rel = path.relative_to(root).as_posix()
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            violations.append((rel, 0, "not valid UTF-8"))
            continue
        # Whole-file text, so a multi-line PEM block is seen, and newline-only line numbers.
        try:
            findings = redact_evidence.shape_findings(text)
        except ValueError:
            violations.append((rel, 0, "redactor changed the line count"))
            findings = []
        violations.extend((rel, number, f"{reason} is not REDACTED") for number, reason in findings)
        for number, line in enumerate(text.split("\n"), start=1):
            violations.extend((rel, number, reason) for reason in pii_violations(line))
    ordered = sorted(violations)
    return [f"{rel}:{n}: {why}" if n else f"{rel}: {why}" for rel, n, why in ordered]


def write_evidence(root: Path, body: str, name: str = "w-test.md") -> None:
    target = root / "docs" / "evidence"
    target.mkdir(parents=True, exist_ok=True)
    (target / name).write_text(body, encoding="utf-8")


def jwt_like() -> str:
    header = base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').decode().rstrip("=")
    return f"{header}.{secrets.token_urlsafe(18)}.{secrets.token_urlsafe(18)}"


def test_repo_evidence_is_clean() -> None:
    assert evidence_violations(REPO) == []


def test_repo_evidence_includes_the_week_1_and_week_2_pages() -> None:
    names = {path.name for path in (REPO / "docs" / "evidence").glob("**/*.md")}
    assert {"w1-prove.md", "w2-spike.md"} <= names


def test_must_fail_fixture_is_reported(tmp_path: Path) -> None:
    lines = [
        f"token: {secrets.token_hex(8)}",
        f"password = {secrets.token_hex(8)}",
        f"s3.session-token={secrets.token_hex(8)}",
        f"<SessionToken>{secrets.token_hex(8)}</SessionToken>",
        f"postgresql://lakekeeper:{secrets.token_hex(8)}@postgres:5432/lakekeeper",
        f"bearer {jwt_like()}",
        "reach me at someone@example.com",
        "call +44 20 7946 0958 today",
        "call 415-555-0132 today",
    ]
    write_evidence(tmp_path, "\n".join(lines) + "\n")
    violations = evidence_violations(tmp_path)
    reported = {int(item.split(":")[1]) for item in violations if item.count(":") >= 2}
    assert reported == set(range(1, len(lines) + 1)), violations


def test_must_pass_fixture_is_clean(tmp_path: Path) -> None:
    lines = [
        "token: REDACTED",
        "password = REDACTED",
        '{"s3.session-token": "REDACTED"}',
        "<SecretAccessKey>REDACTED</SecretAccessKey>",
        "postgresql://lakekeeper:REDACTED@postgres:5432/lakekeeper",
        "tokenDuration: 1h",
        "token_count: 5",
        "sts-token-validity-seconds: 3600",
        "A primary key is not a secret.",
        "Kafka message key: 42",
        "key: 42",
        "Fingerprint: planted-secret.env:shopstream-token:1",
        "Recorded 2026-09-30T12:34:56Z on run 36017988948",
        "sha256:" + "ab" * 32,
        "lsn 0/16B3748 offset +00:00 rows 12,345,678",
        "Secret:      REDACTED",
    ]
    write_evidence(tmp_path, "\n".join(lines) + "\n")
    assert evidence_violations(tmp_path) == []


@pytest.mark.parametrize("value", ["REDACTED1", "redacted", "REDACTEDx", "Redacted"])
def test_only_the_exact_mask_passes(tmp_path: Path, value: str) -> None:
    write_evidence(tmp_path, f"password: {value}\n")
    assert len(evidence_violations(tmp_path)) == 1


def test_an_empty_evidence_tree_has_no_violations(tmp_path: Path) -> None:
    assert evidence_violations(tmp_path) == []
    (tmp_path / "docs" / "evidence").mkdir(parents=True)
    assert evidence_violations(tmp_path) == []


def test_an_empty_credential_value_is_not_reported(tmp_path: Path) -> None:
    write_evidence(tmp_path, "password:\nregion: local\ntoken=\n")
    assert evidence_violations(tmp_path) == []


def test_a_file_that_is_not_utf8_is_reported(tmp_path: Path) -> None:
    target = tmp_path / "docs" / "evidence"
    target.mkdir(parents=True)
    (target / "w-bad.md").write_bytes(b"caf\xe9 \xff\xfe\n")
    assert evidence_violations(tmp_path) == ["docs/evidence/w-bad.md: not valid UTF-8"]


def test_violations_name_the_file_and_line(tmp_path: Path) -> None:
    write_evidence(tmp_path, f"fine\ntoken: {secrets.token_hex(8)}\n", "w-lines.md")
    assert evidence_violations(tmp_path) == [
        "docs/evidence/w-lines.md:2: credential-named field is not REDACTED"
    ]


def test_redactor_output_passes_gate(tmp_path: Path) -> None:
    home = tmp_path / "fake-home" / "someone"
    repo = home / "Code" / "shopstream"
    literal = "envval" + secrets.token_hex(10)
    capture = "\n".join(
        [
            f"cwd {repo}/infra and config {home}/.docker/config.json",
            f"password: {secrets.token_hex(8)}",
            f"AWS_SECRET_ACCESS_KEY={secrets.token_hex(8)}",
            f"database.password={secrets.token_hex(8)}",
            json.dumps(
                {
                    "s3.access-key-id": secrets.token_hex(8),
                    "s3.secret-access-key": secrets.token_hex(8),
                    "s3.session-token": secrets.token_hex(8),
                }
            ),
            f"<AccessKeyId>{secrets.token_hex(8)}</AccessKeyId>",
            f"<SecretAccessKey>{secrets.token_hex(8)}</SecretAccessKey>",
            f"<SessionToken>{jwt_like()}</SessionToken>",
            f"postgresql://lakekeeper:{secrets.token_hex(8)}@postgres:5432/lakekeeper",
            f"Authorization: Bearer {secrets.token_hex(8)}",
            f"assumed with {jwt_like()}",
            f"log line carrying {literal} verbatim",
        ]
    )
    raw_root, clean_root = tmp_path / "raw", tmp_path / "clean"
    write_evidence(raw_root, capture + "\n")
    cleaned = redact_evidence.redact(capture, [literal], repo, home)
    write_evidence(clean_root, cleaned + "\n")

    assert evidence_violations(raw_root) != []
    assert evidence_violations(clean_root) == []
    assert str(home) not in cleaned
    assert str(repo) not in cleaned
    assert literal not in cleaned
    assert "<repo>/infra" in cleaned
    assert "<home>/.docker/config.json" in cleaned


GAP_TEMPLATES = {
    "sts-signing-key": "STS_SIGNING_KEY={v}",
    "seaweedfs-admin-key": "SEAWEEDFS_ADMIN_KEY={v}",
    "api-key-yaml": "api_key: {v}",
    "authorization-header": "Authorization: Bearer {v}",
    "presigned-signature": "https://b.s3/x?X-Amz-Date=20260930T000000Z&X-Amz-Signature={v}",
}
ENV_EXAMPLE_KEYS = list(
    dotenv_lite.parse_dotenv((REPO / "infra" / ".env.example").read_text(encoding="utf-8"))
)


@pytest.mark.parametrize("template", GAP_TEMPLATES.values(), ids=GAP_TEMPLATES.keys())
def test_the_gap_fixtures_are_reported(tmp_path: Path, template: str) -> None:
    line = template.format(v=secrets.token_hex(8))
    reasons = line_violations(line)
    assert reasons
    assert all(reason.endswith("is not REDACTED") for reason in reasons)
    write_evidence(tmp_path, line + "\n")
    assert [item for item in evidence_violations(tmp_path) if ":1:" in item]


@pytest.mark.parametrize("name", ENV_EXAMPLE_KEYS)
def test_every_env_example_key_is_reported_when_unmasked(tmp_path: Path, name: str) -> None:
    write_evidence(tmp_path, f"{name}={secrets.token_hex(8)}\n")
    assert evidence_violations(tmp_path) == [
        "docs/evidence/w-test.md:1: credential-named field is not REDACTED"
    ]


# Built from parts so the source never holds a user:password pair after the HTTP client word.
CLIENT = "cu" + "rl"


def must_fail_lines() -> dict[str, str]:
    """Raw lines the gate must report: the shapes reproduced in review (CR-01, WR-01, WR-02)."""
    value = secrets.token_hex(8)
    first, second = secrets.token_hex(6), secrets.token_hex(6)
    return {
        "query-string": f"http://h/x?user=bob&password={value}",
        "logfmt-message": f'level=error msg="connect failed password={value} host=x"',
        "curl-header": f"curl -H 'Authorization: Bearer {value}' http://x",
        "cookie": f"Cookie: session={value}; theme=dark",
        "space-separated-flag": f"postgres --password {value} -h db",
        "partially-masked": f"password: REDACTED {first} {second}",
        "bare-bearer": f"using Bearer {value} now",
        "dsn-password-with-at": f"postgresql://u:{first}@{second}@host/db",
        "comma-in-value": f"PGPASSWORD={first},{second}",
        # G-01-3: the shapes the redactor closed after the first review round.
        "dbt-secret-env": f"DBT_ENV_SECRET_PG_USER={value}",
        "db-pass": f"DB_PASS={value}",
        "odbc-pwd": f"Server=db;Uid=bob;Pwd={value};",
        "yaml-doubled-quote": f"password: '{first}''{second}'",
        "yaml-leaked-tail": f"password: 'REDACTED''{second}'",
        "flag-doubled-quote": f"cli --password '{first}''{second}'",
        "http-client-user": CLIENT + f" -u bob:{value} http://x",
        "http-client-long-user": CLIENT + f" --user bob:{value} http://x",
        "registry-login": f"docker login -u bob -p {value} registry.example",
        "registry-login-after-mkdir": f"mkdir -p /tmp/x && docker login -u bob -p {value} reg",
        # G-01-1: a secret flag glued after a dot is still reported.
        "flag-after-ellipsis": f"...--password {value} -h db",
        "flag-after-abbreviation": f"e.g.--token {value}",
    }


def must_pass_texts() -> dict[str, str]:
    """Field names, prose and exact masks the gate must not report. A value may span lines."""
    certificate_body = base64.b64encode(secrets.token_bytes(48)).decode()
    return {
        "unique-key": "unique_key: order_id",
        "signed-headers": "SignedHeaders=host;x-amz-date",
        "amz-date": "X-Amz-Date=20260930T000000Z",
        "signature-version": "signature_version: s3v4",
        "password-stdin": "docker login --password-stdin",
        "no-password": "psql --no-password -h db",
        "certificate-block": f"-----BEGIN CERTIFICATE-----\n{certificate_body}\n-----END CERTIFICATE-----",
        "authorization-prose": "The Authorization header carries a SigV4 signature.",
        "authorization-endpoint": "authorization_endpoint: http://127.0.0.1:8181/x",
        "partition-key": "partition key: 7",
        "bearer-of-news": "a bearer of news",
        "mask-with-trailing-spaces": "password: REDACTED   ",
        "masked-authorization": "Authorization: REDACTED",
        "masked-bearer": "Bearer REDACTED",
        "bypass": "bypass: true",
        "compass": "compass: north",
        "empty-single-quoted": "password: ''",
        "masked-single-quoted": "password: 'REDACTED'",
        "psql-user-flag": "psql -U postgres -d shopstream",
        "compose-project-flag": "docker compose -p shopstream-uat up -d",
        "run-port-and-user": "docker run -p 127.0.0.1:8080:8080 -u 1000:1000 img",
        "mkdir-parents": "mkdir -p /tmp/x",
        "login-password-stdin-with-user": "docker login -u bob --password-stdin",
        "http-client-user-only": CLIENT + " -u bob http://x",
        "masked-http-client-user": CLIENT + " -u bob:REDACTED http://x",
        "masked-registry-login": "docker login -u bob -p REDACTED reg",
    }


def fake_paths(root: Path) -> tuple[Path, Path]:
    home = root / "fake-home" / "someone"
    return home / "Code" / "shopstream", home


MUST_FAIL = must_fail_lines()
MUST_PASS = must_pass_texts()


@pytest.mark.parametrize("raw", MUST_FAIL.values(), ids=MUST_FAIL.keys())
def test_reproduced_leak_shapes_fail_raw_and_pass_redacted(tmp_path: Path, raw: str) -> None:
    repo, home = fake_paths(tmp_path)
    raw_root, clean_root = tmp_path / "raw", tmp_path / "clean"
    write_evidence(raw_root, raw + "\n")
    write_evidence(clean_root, redact_evidence.redact(raw, [], repo, home) + "\n")
    assert [item for item in evidence_violations(raw_root) if ":1:" in item]
    assert evidence_violations(clean_root) == []


def test_a_pem_private_key_reports_each_body_line(tmp_path: Path) -> None:
    body = [base64.b64encode(secrets.token_bytes(48)).decode() for _ in range(2)]
    text = "\n".join(["-----BEGIN PRIVATE KEY-----", *body, "-----END PRIVATE KEY-----"]) + "\n"
    write_evidence(tmp_path / "raw", text)
    assert evidence_violations(tmp_path / "raw") == [
        f"docs/evidence/w-test.md:{number}: PEM private key is not REDACTED" for number in (2, 3)
    ]
    write_evidence(tmp_path / "clean", redact_evidence.redact_shapes(text))
    assert evidence_violations(tmp_path / "clean") == []


@pytest.mark.parametrize("text", MUST_PASS.values(), ids=MUST_PASS.keys())
def test_field_names_and_prose_pass(tmp_path: Path, text: str) -> None:
    write_evidence(tmp_path, text + "\n")
    assert evidence_violations(tmp_path) == []


def test_a_continued_command_reports_only_the_credential_line(tmp_path: Path) -> None:
    value = secrets.token_hex(8)
    raw = CLIENT + " -sS \\\n  -u bob:" + value + " \\\n  http://x\n"
    write_evidence(tmp_path / "raw", raw)
    assert evidence_violations(tmp_path / "raw") == [
        "docs/evidence/w-test.md:2: secret CLI flag is not REDACTED"
    ]
    write_evidence(tmp_path / "clean", redact_evidence.redact_shapes(raw))
    assert evidence_violations(tmp_path / "clean") == []


def test_a_partially_masked_value_is_reported(tmp_path: Path) -> None:
    write_evidence(tmp_path, f"password: REDACTED {secrets.token_hex(4)} {secrets.token_hex(4)}\n")
    assert evidence_violations(tmp_path) == [
        "docs/evidence/w-test.md:1: credential-named field is not REDACTED"
    ]


@pytest.mark.parametrize("name", ["API_KEY", "api-key", "apiKey", "Api.Key"])
def test_credential_names_match_across_case_and_separators(tmp_path: Path, name: str) -> None:
    write_evidence(tmp_path, f"{name}={secrets.token_hex(8)}\n")
    assert evidence_violations(tmp_path) == [
        "docs/evidence/w-test.md:1: credential-named field is not REDACTED"
    ]


def test_line_numbers_count_newlines_only(tmp_path: Path) -> None:
    write_evidence(tmp_path, f"page\x0cbreak\ntoken: {secrets.token_hex(8)}\n", "w-lines.md")
    assert evidence_violations(tmp_path) == [
        "docs/evidence/w-lines.md:2: credential-named field is not REDACTED"
    ]


def test_an_empty_evidence_file_has_no_violations(tmp_path: Path) -> None:
    write_evidence(tmp_path, "")
    assert evidence_violations(tmp_path) == []


def test_a_masked_dsn_is_not_an_email(tmp_path: Path) -> None:
    write_evidence(tmp_path, "postgresql://lakekeeper:REDACTED@db.example.com/lakekeeper\n")
    assert evidence_violations(tmp_path) == []
    write_evidence(tmp_path, "reach me at someone@example.com\n")
    assert evidence_violations(tmp_path) == ["docs/evidence/w-test.md:1: email address"]


def test_gate_flags_exactly_what_the_redactor_would_change() -> None:
    samples = [*MUST_FAIL.values(), *MUST_PASS.values()]
    assert any(redact_evidence.redact_shapes(sample) != sample for sample in MUST_FAIL.values())
    for sample in samples:
        for line in sample.split("\n"):
            shape_reasons = [r for r in line_violations(line) if r.endswith("is not REDACTED")]
            assert bool(shape_reasons) == (redact_evidence.redact_shapes(line) != line), line
        whole = bool(redact_evidence.shape_findings(sample))
        assert whole == (redact_evidence.redact_shapes(sample) != sample), sample
