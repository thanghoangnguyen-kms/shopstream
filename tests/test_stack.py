"""Docker-free unit tests for scripts/stack.py.

Every fake secret is generated at run time, so gitleaks never sees a literal, and none is
bound to a name ruff's S105 or S106 would treat as a password.
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import stack
from dotenv_lite import DotenvError, parse_dotenv

EXAMPLE = stack.ENV_EXAMPLE.read_text(encoding="utf-8")
TEMPLATE = stack.IDENTITY_TEMPLATE.read_text(encoding="utf-8")
KEYS = list(parse_dotenv(EXAMPLE))
LAKEKEEPER_ARN = "arn:aws:iam::000000000000:user/lakekeeper"
WAREHOUSE_RESOURCES = {"arn:aws:s3:::warehouse", "arn:aws:s3:::warehouse/*"}
MEASURED_MEM_TOTAL = 12515225600
GIB = 2**30
HEX64 = re.compile(r"^[0-9a-f]{64}$")
CANARY = re.compile(r"^shpst_[0-9a-f]{40}$")
FERNET = re.compile(r"^[A-Za-z0-9_-]{43}=$")


def shape_of(key: str) -> re.Pattern[str]:
    """The pattern a generated value for `key` must match."""
    if key == "CANARY_TOKEN":
        return CANARY
    if key == "AIRFLOW_FERNET_KEY":
        return FERNET
    return HEX64


def runtime_env() -> dict[str, str]:
    return {key: secrets.token_hex(32) for key in KEYS}


def walk(node: object) -> list[object]:
    """Every value in a JSON document, containers included."""
    found: list[object] = [node]
    if isinstance(node, dict):
        for child in node.values():
            found += walk(child)
    elif isinstance(node, list):
        for child in node:
            found += walk(child)
    return found


# --- preflight ---------------------------------------------------------------------------


def test_min_vm_bytes_is_the_measured_mem_total_rounded_down_to_256_mib() -> None:
    assert stack.MIN_VM_BYTES == 12348030976
    assert stack.MIN_VM_BYTES == MEASURED_MEM_TOTAL // (256 * 2**20) * (256 * 2**20)


@pytest.mark.parametrize(
    ("mem_total", "passes"),
    [
        (MEASURED_MEM_TOTAL, True),
        (stack.MIN_VM_BYTES, True),
        (stack.MIN_VM_BYTES - 1, False),
        (8 * GIB, False),
    ],
)
def test_preflight_verdict_boundary(mem_total: int, passes: bool) -> None:
    ok, message = stack.preflight_verdict(mem_total, stack.MIN_VM_BYTES)
    assert ok is passes
    assert str(mem_total) in message
    assert str(stack.MIN_VM_BYTES) in message
    assert ("below threshold" in message) is (not passes)


def test_preflight_compares_integers_exactly() -> None:
    # A float conversion would round these two to the same value.
    big = 2**60
    assert stack.preflight_verdict(big, big)[0] is True
    assert stack.preflight_verdict(big - 1, big)[0] is False


def test_read_mem_total_parses_the_docker_info_output(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []

    def fake(argv: list[str]) -> tuple[int, str]:
        seen.append(argv)
        return 0, f"{MEASURED_MEM_TOTAL}\n"

    monkeypatch.setattr(stack, "run_capture", fake)
    assert stack.read_mem_total() == MEASURED_MEM_TOTAL
    assert seen == [["docker", "info", "--format", "{{.MemTotal}}"]]


@pytest.mark.parametrize("result", [(1, ""), (127, ""), (0, "not a number"), (0, ""), (0, "-5")])
def test_read_mem_total_failure_names_colima_and_its_start_command(
    monkeypatch: pytest.MonkeyPatch, result: tuple[int, str]
) -> None:
    monkeypatch.setattr(stack, "run_capture", lambda argv: result)
    with pytest.raises(stack.PreflightError, match="Colima") as info:
        stack.read_mem_total()
    assert "colima start --vm-type vz --memory 12 --cpu 4" in str(info.value)


# --- sandbox: the whole command against a tmp repo, with Docker faked ----------------------


@dataclass
class Sandbox:
    root: Path
    calls: list[tuple[list[str], dict[str, str]]] = field(default_factory=list)
    exit_codes: list[int] = field(default_factory=list)

    @property
    def env_file(self) -> Path:
        return self.root / "infra" / ".env"

    @property
    def identity_file(self) -> Path:
        return self.root / "infra" / ".generated" / "seaweedfs" / "iam.json"

    @property
    def lock_file(self) -> Path:
        return self.root / "infra" / ".generated" / "stack.lock"

    def argvs(self) -> list[list[str]]:
        return [argv for argv, _ in self.calls]


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Sandbox:
    box = Sandbox(tmp_path)
    infra = tmp_path / "infra"
    (infra / "seaweedfs").mkdir(parents=True)
    (infra / ".env.example").write_text(EXAMPLE, encoding="utf-8")
    (infra / "seaweedfs" / "iam.json.tmpl").write_text(TEMPLATE, encoding="utf-8")
    monkeypatch.setattr(stack, "ENV_EXAMPLE", infra / ".env.example")
    monkeypatch.setattr(stack, "ENV_FILE", box.env_file)
    monkeypatch.setattr(stack, "IDENTITY_TEMPLATE", infra / "seaweedfs" / "iam.json.tmpl")
    monkeypatch.setattr(stack, "IDENTITY_FILE", box.identity_file)
    monkeypatch.setattr(stack, "ENV_LOCK_FILE", box.lock_file)
    monkeypatch.setattr(stack, "read_mem_total", lambda: MEASURED_MEM_TOTAL)
    monkeypatch.delenv("COMPOSE_PROFILES", raising=False)

    def fake_docker(argv: list[str], env: Mapping[str, str]) -> int:
        box.calls.append((argv, dict(env)))
        return box.exit_codes.pop(0) if box.exit_codes else 0

    monkeypatch.setattr(stack, "run_docker", fake_docker)
    return box


def test_a_failing_preflight_touches_no_file_and_runs_no_compose_call(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(stack, "read_mem_total", lambda: 8 * GIB)
    assert stack.cmd_up() == 1
    assert not sandbox.env_file.exists()
    assert not sandbox.identity_file.parent.exists()
    assert sandbox.calls == []
    out = capsys.readouterr().out
    assert str(8 * GIB) in out
    assert str(stack.MIN_VM_BYTES) in out


def test_an_unreadable_vm_exits_1_through_main(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def broken() -> int:
        raise stack.PreflightError("no daemon; start Colima")

    monkeypatch.setattr(stack, "read_mem_total", broken)
    assert stack.main(["up"]) == 1
    assert "no daemon" in capsys.readouterr().err
    assert not sandbox.env_file.exists()
    assert sandbox.calls == []


def test_up_runs_preflight_env_identity_then_three_compose_steps(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stack.cmd_up() == 0
    out = capsys.readouterr().out
    assert out.index("VM memory ok") < out.index("+ docker compose")
    assert len(sandbox.calls) == 3
    values = parse_dotenv(sandbox.env_file.read_text(encoding="utf-8"))
    assert list(values) == KEYS
    assert sandbox.identity_file.is_file()
    for value in values.values():
        assert value not in out


def test_up_prints_key_names_and_counts_but_no_generated_secret(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    stack.cmd_up()
    out = capsys.readouterr().out
    assert f"added {len(KEYS)} keys" in out
    assert "POSTGRES_PASSWORD" in out
    assert not re.search(r"[0-9a-f]{40}", out)


def test_up_stops_at_the_first_failing_step(sandbox: Sandbox) -> None:
    sandbox.exit_codes = [0, 3]
    assert stack.cmd_up() == 3
    assert len(sandbox.calls) == 2


def test_up_gives_docker_a_clean_child_environment(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "streaming,bootstrap")
    stack.cmd_up()
    up, run, logs = sandbox.argvs()
    assert [a for a in up if a in {"core", "streaming", "bootstrap"}] == ["core", "streaming"]
    assert "bootstrap" in run
    assert "--remove-orphans" not in up + run + logs
    for _, env in sandbox.calls:
        assert env["COMPOSE_IGNORE_ORPHANS"] == "true"
        assert "COMPOSE_PROFILES" not in env


def test_an_invalid_profile_name_is_rejected_before_any_docker_call(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("COMPOSE_PROFILES", "x;rm")
    assert stack.main(["up"]) == 1
    assert "invalid compose profile" in capsys.readouterr().err
    assert sandbox.calls == []


# --- infra/.env init and top-up ------------------------------------------------------------


def test_a_fresh_env_fills_every_empty_value_and_keeps_comments_and_order() -> None:
    text, added = stack.init_env(EXAMPLE, "")
    values = parse_dotenv(text)
    assert added == KEYS
    assert list(values) == KEYS
    for key, value in values.items():
        assert shape_of(key).match(value), key
    assert len(set(values.values())) == len(KEYS)
    comments = [line for line in text.splitlines() if line.startswith("#")]
    assert comments == [line for line in EXAMPLE.splitlines() if line.startswith("#")]


def test_the_fernet_key_is_the_urlsafe_base64_of_32_random_bytes() -> None:
    key = stack.generate_value(stack.FERNET_KEY_NAME)
    assert stack.FERNET_KEY_NAME == "AIRFLOW_FERNET_KEY"
    assert len(base64.urlsafe_b64decode(key)) == 32
    assert stack.generate_value(stack.FERNET_KEY_NAME) != key
    # The default branch stays 64 hex characters, which is not a Fernet key.
    assert HEX64.match(stack.generate_value("CDC_DB_PASSWORD"))


def test_env_creates_the_file_then_tops_up_and_runs_no_docker_command(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    assert stack.main(["env"]) == 0
    first = capsys.readouterr().out
    assert f"env file: added {len(KEYS)} keys" in first
    assert "identity file: rendered" in first
    values = parse_dotenv(sandbox.env_file.read_text(encoding="utf-8"))
    assert list(values) == KEYS
    assert sandbox.identity_file.is_file()
    removed = "CDC_DB_PASSWORD"
    text = "".join(
        line + "\n"
        for line in sandbox.env_file.read_text(encoding="utf-8").splitlines()
        if not line.startswith(f"{removed}=")
    )
    sandbox.env_file.write_text(text, encoding="utf-8")
    assert stack.main(["env"]) == 0
    second = capsys.readouterr().out
    assert f"env file: added 1 keys: {removed}" in second
    assert stack.main(["env"]) == 0
    assert "env file: up to date" in capsys.readouterr().out
    assert sandbox.calls == []
    assert list(parse_dotenv(sandbox.env_file.read_text(encoding="utf-8"))) == [
        key for key in KEYS if key != removed
    ] + [removed]
    for value in values.values():
        assert value not in first + second


def test_write_env_fresh_is_exclusive_and_private(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    stack.write_env_fresh(path, "A=1\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        stack.write_env_fresh(path, "A=2\n")
    assert path.read_text(encoding="utf-8") == "A=1\n"


def test_top_up_keeps_existing_values_and_appends_only_missing_keys_in_order() -> None:
    kept = secrets.token_hex(8)
    existing = f"{KEYS[0]}={kept}\n"
    text, added = stack.init_env(EXAMPLE, existing)
    assert text.startswith(existing)
    assert added == KEYS[1:]
    values = parse_dotenv(text)
    assert values[KEYS[0]] == kept
    assert list(values) == KEYS


def test_top_up_of_a_file_without_a_trailing_newline_keeps_both_lines() -> None:
    kept = secrets.token_hex(8)
    text, _ = stack.init_env(EXAMPLE, f"{KEYS[0]}={kept}")
    assert parse_dotenv(text)[KEYS[0]] == kept
    assert list(parse_dotenv(text)) == KEYS


def test_a_complete_env_comes_back_byte_identical() -> None:
    complete, _ = stack.init_env(EXAMPLE, "")
    again, added = stack.init_env(EXAMPLE, complete)
    assert again == complete
    assert added == []


def test_a_duplicate_key_in_the_env_names_the_key_and_never_the_value() -> None:
    first, second = secrets.token_hex(8), secrets.token_hex(8)
    with pytest.raises(DotenvError, match=KEYS[0]) as info:
        stack.init_env(EXAMPLE, f"{KEYS[0]}={first}\n{KEYS[0]}={second}\n")
    assert first not in str(info.value)
    assert second not in str(info.value)


def test_a_duplicate_key_in_the_example_is_rejected() -> None:
    with pytest.raises(DotenvError, match="ALPHA"):
        stack.init_env("ALPHA=\nALPHA=\n", "")


def test_sync_env_creates_then_tops_up(sandbox: Sandbox) -> None:
    assert stack.sync_env() == KEYS
    assert stat.S_IMODE(sandbox.env_file.stat().st_mode) == 0o600
    before = sandbox.env_file.read_text(encoding="utf-8")
    assert stack.sync_env() == []
    assert sandbox.env_file.read_text(encoding="utf-8") == before
    partial = "".join(line + "\n" for line in before.splitlines()[:4])
    sandbox.env_file.write_text(partial, encoding="utf-8")
    assert stack.sync_env() == [key for key in KEYS if key not in parse_dotenv(partial)]
    assert sandbox.env_file.read_text(encoding="utf-8").startswith(partial)


def test_a_concurrent_first_run_is_topped_up_never_clobbered(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    winner_text, _ = stack.init_env(EXAMPLE, "")

    def lose_the_race(path: Path, text: str) -> None:
        path.write_text(winner_text, encoding="utf-8")  # the other run got there first
        raise FileExistsError

    monkeypatch.setattr(stack, "write_env_fresh", lose_the_race)
    assert stack.sync_env() == []
    assert sandbox.env_file.read_text(encoding="utf-8") == winner_text


def test_up_from_a_copy_of_the_example_fills_every_value_and_renders_the_identity(
    sandbox: Sandbox, capsys: pytest.CaptureFixture[str]
) -> None:
    sandbox.env_file.write_bytes(stack.ENV_EXAMPLE.read_bytes())
    sandbox.env_file.chmod(0o644)
    assert stack.main(["up"]) == 0
    assert stat.S_IMODE(sandbox.env_file.stat().st_mode) == 0o600
    text = sandbox.env_file.read_text(encoding="utf-8")
    values = parse_dotenv(text)
    assert list(values) == KEYS
    for key, value in values.items():
        assert shape_of(key).match(value), key
    assert len(set(values.values())) == len(KEYS)
    comments = [line for line in text.splitlines() if line.startswith("#")]
    assert comments == [line for line in EXAMPLE.splitlines() if line.startswith("#")]
    assert isinstance(json.loads(sandbox.identity_file.read_text(encoding="utf-8")), dict)
    assert len(sandbox.calls) == 3
    captured = capsys.readouterr()
    for value in values.values():
        assert value not in captured.out + captured.err


def test_init_env_fills_every_empty_value_of_a_copied_example() -> None:
    text, added = stack.init_env(EXAMPLE, EXAMPLE)
    assert added == KEYS
    values = parse_dotenv(text)
    assert list(values) == KEYS
    assert all(values.values())


def test_init_env_fills_an_empty_value_in_place_and_keeps_set_values_byte_for_byte() -> None:
    kept = secrets.token_hex(8)
    existing = f"{KEYS[0]}={kept}\n{KEYS[1]}=\n"
    text, added = stack.init_env(EXAMPLE, existing)
    assert added == KEYS[1:]
    lines = text.splitlines(keepends=True)
    assert lines[0] == f"{KEYS[0]}={kept}\n"
    assert lines[1].startswith(f"{KEYS[1]}=")
    assert lines[1].endswith("\n")
    assert len([line for line in lines if line.startswith(f"{KEYS[1]}=")]) == 1
    values = parse_dotenv(text)
    assert values[KEYS[0]] == kept
    assert values[KEYS[1]]
    assert list(values) == KEYS


def test_sync_env_makes_an_existing_complete_file_private_without_changing_it(
    sandbox: Sandbox,
) -> None:
    complete, _ = stack.init_env(EXAMPLE, "")
    sandbox.env_file.write_text(complete, encoding="utf-8")
    sandbox.env_file.chmod(0o644)
    assert stack.sync_env() == []
    assert sandbox.env_file.read_text(encoding="utf-8") == complete
    assert stat.S_IMODE(sandbox.env_file.stat().st_mode) == 0o600


def locked_elsewhere(path: Path) -> bool:
    """True while another open file description holds the exclusive lock on `path`."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    finally:
        os.close(fd)  # closing also drops a lock this probe just took
    return False


def test_a_whitespace_only_env_file_is_written_whole(sandbox: Sandbox) -> None:
    sandbox.env_file.write_text("\n\n", encoding="utf-8")
    assert stack.sync_env() == KEYS
    text = sandbox.env_file.read_text(encoding="utf-8")
    assert text.splitlines()[0] == EXAMPLE.splitlines()[0]
    assert list(parse_dotenv(text)) == KEYS
    assert all(parse_dotenv(text).values())


def test_crlf_line_endings_survive_a_top_up(sandbox: Sandbox) -> None:
    complete, _ = stack.init_env(EXAMPLE, "")
    lines = complete.splitlines()
    index = next(i for i, line in enumerate(lines) if line.startswith(f"{KEYS[3]}="))
    lines[index] = f"{KEYS[3]}="
    sandbox.env_file.write_bytes("".join(line + "\r\n" for line in lines).encode())
    assert stack.sync_env() == [KEYS[3]]
    data = sandbox.env_file.read_bytes()
    assert data.count(b"\n") == data.count(b"\r\n") == len(lines)
    after = data.decode().splitlines(keepends=True)
    assert after[index].startswith(f"{KEYS[3]}=")
    assert after[index].endswith("\r\n")
    assert len(after[index]) > len(f"{KEYS[3]}=\r\n")
    assert [line for i, line in enumerate(after) if i != index] == [
        line + "\r\n" for i, line in enumerate(lines) if i != index
    ]


def test_spaced_and_unterminated_empty_keys_are_filled_once() -> None:
    kept = secrets.token_hex(4)
    existing = f"{KEYS[0]} =\n{KEYS[1]}= \n{KEYS[2]}={kept}\nCANARY_TOKEN="
    text, added = stack.init_env(EXAMPLE, existing)
    assert added == [key for key in KEYS if key != KEYS[2]]
    lines = text.splitlines()
    assert len(lines) == len(KEYS)
    assert lines[3].startswith("CANARY_TOKEN=")
    assert lines[4].startswith(f"{KEYS[3]}=")
    for key in KEYS:
        assert len([line for line in lines if line.partition("=")[0].strip() == key]) == 1
    values = parse_dotenv(text)
    assert values[KEYS[2]] == kept
    assert all(values.values())


def test_keys_outside_the_example_are_left_alone() -> None:
    kept = secrets.token_hex(4)
    existing = f"EXTRA=\nOTHER={kept}\n{KEYS[0]}=\n"
    text, added = stack.init_env(EXAMPLE, existing)
    assert text.startswith(f"EXTRA=\nOTHER={kept}\n")
    assert "EXTRA" not in added
    assert "OTHER" not in added
    values = parse_dotenv(text)
    assert values["EXTRA"] == ""
    assert values["OTHER"] == kept
    assert values[KEYS[0]]


def test_added_keys_come_back_in_example_order() -> None:
    a, b = secrets.token_hex(4), secrets.token_hex(4)
    existing = f"{KEYS[1]}={a}\n{KEYS[2]}=\n" + "".join(f"{key}={b}\n" for key in KEYS[3:])
    text, added = stack.init_env(EXAMPLE, existing)
    assert added == [KEYS[0], KEYS[2]]
    lines = text.splitlines()
    assert lines[0] == f"{KEYS[1]}={a}"
    assert lines[1].startswith(f"{KEYS[2]}=")
    assert lines[1] != f"{KEYS[2]}="
    assert lines[-1].startswith(f"{KEYS[0]}=")


def test_a_failed_rewrite_keeps_the_old_file_and_leaves_no_temp_file(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    kept = secrets.token_hex(4)
    old = f"{KEYS[0]}={kept}\n"
    sandbox.env_file.write_text(old, encoding="utf-8")
    before = sorted(child.name for child in sandbox.env_file.parent.iterdir())

    def fail(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        stack.sync_env()
    assert sandbox.env_file.read_text(encoding="utf-8") == old
    assert sorted(child.name for child in sandbox.env_file.parent.iterdir()) == before


def test_env_lock_is_exclusive_until_released(tmp_path: Path) -> None:
    path = tmp_path / ".generated" / "stack.lock"
    with stack.env_lock(path):
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert locked_elsewhere(path)
    assert not locked_elsewhere(path)
    assert path.exists()  # kept, never deleted: deleting it would reopen the race


def test_up_tops_up_and_renders_under_the_lock(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, bool] = {}
    real_sync, real_write = stack.sync_env, stack.write_identity

    def sync() -> list[str]:
        seen["sync"] = locked_elsewhere(sandbox.lock_file)
        return real_sync()

    def write(path: Path, text: str) -> None:
        seen["identity"] = locked_elsewhere(sandbox.lock_file)
        real_write(path, text)

    def docker(argv: list[str], env: Mapping[str, str]) -> int:
        seen["compose"] = locked_elsewhere(sandbox.lock_file)
        return 0

    monkeypatch.setattr(stack, "sync_env", sync)
    monkeypatch.setattr(stack, "write_identity", write)
    monkeypatch.setattr(stack, "run_docker", docker)
    assert stack.cmd_up() == 0
    assert seen == {"sync": True, "identity": True, "compose": False}
    assert stat.S_IMODE(sandbox.lock_file.stat().st_mode) == 0o600


def test_a_failing_preflight_creates_no_lock_file(
    sandbox: Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(stack, "read_mem_total", lambda: 8 * GIB)
    assert stack.cmd_up() == 1
    assert not sandbox.lock_file.exists()
    assert not sandbox.lock_file.parent.exists()


# --- the identity file -----------------------------------------------------------------------


def test_the_rendered_identity_has_exactly_the_planned_identities_and_one_role() -> None:
    document = json.loads(stack.render_identity(TEMPLATE, runtime_env()))
    identities = {item["name"]: item for item in document["identities"]}
    assert set(identities) == {"admin", "lakekeeper", "probe-other"}
    assert identities["lakekeeper"]["actions"] == ["Read", "List", "Tagging", "Write"]
    assert {name for name, item in identities.items() if "Admin" in item["actions"]} == {"admin"}
    assert [role["roleName"] for role in document["roles"]] == ["LakekeeperVendedRole"]


def test_the_trust_policy_names_only_the_lakekeeper_user_as_an_exact_string() -> None:
    document = json.loads(stack.render_identity(TEMPLATE, runtime_env()))
    principals = [
        statement["Principal"]
        for role in document["roles"]
        for statement in role["trustPolicy"]["Statement"]
    ]
    assert principals == [{"AWS": LAKEKEEPER_ARN}]
    assert principals[0]["AWS"] != LAKEKEEPER_ARN.upper()
    assert "*" not in walk(document)


def test_the_vended_policy_covers_only_the_warehouse_bucket() -> None:
    document = json.loads(stack.render_identity(TEMPLATE, runtime_env()))
    resources = {
        resource
        for policy in document["policies"]
        for statement in policy["document"]["Statement"]
        for resource in statement["Resource"]
    }
    assert resources == WAREHOUSE_RESOURCES


def test_the_sts_signing_key_decodes_to_at_least_16_bytes() -> None:
    document = json.loads(stack.render_identity(TEMPLATE, runtime_env()))
    assert len(base64.b64decode(document["sts"]["signingKey"], validate=True)) >= 16


@pytest.mark.parametrize("name", sorted(set(re.findall(r"\$\{(\w+)\}", TEMPLATE))))
@pytest.mark.parametrize("empty", [False, True])
def test_a_missing_or_empty_variable_is_named_and_no_value_is_leaked(
    name: str, empty: bool
) -> None:
    env = runtime_env()
    if empty:
        env[name] = ""
    else:
        del env[name]
    with pytest.raises(stack.EnvInitError, match=name) as info:
        stack.render_identity(TEMPLATE, env)
    assert not [value for value in env.values() if value and value in str(info.value)]


def test_a_short_signing_key_is_rejected_by_decoded_length() -> None:
    env = runtime_env()
    env["STS_SIGNING_KEY"] = base64.b64encode(secrets.token_bytes(15)).decode()
    with pytest.raises(stack.EnvInitError, match="at least 16 bytes"):
        stack.render_identity(TEMPLATE, env)
    env["STS_SIGNING_KEY"] = base64.b64encode(secrets.token_bytes(16)).decode()
    stack.render_identity(TEMPLATE, env)


def test_a_signing_key_that_is_not_base64_is_rejected() -> None:
    env = runtime_env()
    env["STS_SIGNING_KEY"] = "not*base64!"
    with pytest.raises(stack.EnvInitError, match="base64"):
        stack.render_identity(TEMPLATE, env)


def test_write_identity_is_private_atomic_and_repeatable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".generated" / "seaweedfs" / "iam.json"
    text = stack.render_identity(TEMPLATE, runtime_env())
    real_replace = os.replace
    replaced: list[tuple[Path, Path]] = []

    def spy(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        replaced.append((Path(source), Path(target)))
        real_replace(source, target)

    monkeypatch.setattr(os, "replace", spy)
    stack.write_identity(path, text)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    ((source, target),) = replaced
    assert target == path
    assert source.parent == path.parent
    assert source != path
    first = path.read_bytes()
    stack.write_identity(path, text)
    assert path.read_bytes() == first
    assert sorted(child.name for child in path.parent.iterdir()) == ["iam.json"]


def test_write_identity_leaves_no_temp_file_when_the_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / ".generated" / "seaweedfs" / "iam.json"

    def fail(source: str | os.PathLike[str], target: str | os.PathLike[str]) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        stack.write_identity(path, "{}")
    assert list(path.parent.iterdir()) == []


# --- profiles and argv ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (None, ["core"]),
        ("", ["core"]),
        ("streaming,orchestration", ["core", "streaming", "orchestration"]),
        (" streaming , ,orchestration,streaming", ["core", "streaming", "orchestration"]),
        ("bootstrap,*,core,streaming", ["core", "streaming"]),
        ("spike,streaming", ["core", "streaming"]),
        ("bootstrap,spike,*", ["core"]),
    ],
)
def test_parse_profiles(value: str | None, expected: list[str]) -> None:
    env = {} if value is None else {"COMPOSE_PROFILES": value}
    assert stack.parse_profiles(env) == expected


@pytest.mark.parametrize("bad", ["x;rm", "Upper", "1st", "a b", "$(id)", "-lead"])
def test_parse_profiles_rejects_names_that_could_shape_the_command(bad: str) -> None:
    with pytest.raises(ValueError, match="invalid compose profile"):
        stack.parse_profiles({"COMPOSE_PROFILES": f"streaming,{bad}"})


def test_compose_argv_passes_one_profile_flag_per_profile_in_order() -> None:
    argv = stack.compose_argv(["core", "streaming"], "ps")
    assert argv == [
        "docker",
        "compose",
        "-f",
        str(stack.COMPOSE_FILE),
        "--profile",
        "core",
        "--profile",
        "streaming",
        "ps",
    ]


def test_up_steps_are_up_then_run_warehouse_then_bootstrap_logs() -> None:
    up, run, logs = stack.up_steps(["core", "streaming"])
    assert up[-4:] == ["up", "--wait", "--wait-timeout", "300"]
    assert "bootstrap" not in up
    assert run[-3:] == ["run", "--rm", "warehouse"]
    assert run.count("--profile") == 3
    assert run[run.index("core") - 1 :][:6] == [
        "--profile",
        "core",
        "--profile",
        "streaming",
        "--profile",
        "bootstrap",
    ]
    assert logs[-3:] == ["logs", "--no-log-prefix", "bootstrap"]
    assert "bootstrap" in logs[:-3]


def test_down_removes_orphans_of_every_profile_and_keeps_volumes_by_default() -> None:
    plain = stack.down_argv(volumes=False)
    assert plain[-4:] == ["--profile", "*", "down", "--remove-orphans"]
    assert "--volumes" not in plain
    assert stack.down_argv(volumes=True)[-1] == "--volumes"


def test_child_env_ignores_orphans_and_drops_the_callers_profiles() -> None:
    env = stack.child_env({"COMPOSE_PROFILES": "x", "PATH": "/bin"}, KEYS, env_file_exists=True)
    assert env["COMPOSE_IGNORE_ORPHANS"] == "true"
    assert "COMPOSE_PROFILES" not in env
    assert env["PATH"] == "/bin"
    assert not [key for key in KEYS if key in env]


def test_child_env_supplies_every_key_when_there_is_no_env_file() -> None:
    env = stack.child_env({}, KEYS, env_file_exists=False)
    assert all(env[key] for key in KEYS)


def test_child_env_never_overrides_a_value_the_caller_set() -> None:
    mine = secrets.token_hex(4)
    env = stack.child_env({KEYS[0]: mine}, KEYS, env_file_exists=False)
    assert env[KEYS[0]] == mine


def test_down_needs_no_env_file(sandbox: Sandbox) -> None:
    assert stack.cmd_down(volumes=False) == 0
    ((argv, env),) = sandbox.calls
    assert "down" in argv
    assert "--volumes" not in argv
    assert all(env[key] for key in KEYS)
    assert not sandbox.env_file.exists()


def test_down_volumes_flag_reaches_compose(sandbox: Sandbox) -> None:
    assert stack.main(["down", "--volumes"]) == 0
    assert sandbox.argvs()[0][-1] == "--volumes"
