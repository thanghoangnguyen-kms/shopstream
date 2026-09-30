"""Bring the Shopstream stack up and down: `just up` and `just down`.

`up` refuses a VM that is too small, creates or tops up infra/.env, renders the SeaweedFS
identity file into the git-ignored infra/.generated/, brings the core profile up healthy and
runs the bootstrap and warehouse one-shots. `down` removes every profile's containers.

The top-up fills every empty value in an existing infra/.env (so a plain copy of
.env.example works), appends the keys the file lacks, never changes a value that is set and
keeps the file at mode 0600.

The planning functions are pure and tested without Docker; the small I/O functions at the
bottom are what tests replace. Nothing here prints a value from infra/.env or the output of
`docker compose config`: the values are secrets.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
import re
import secrets
import string
import subprocess
import sys
import tempfile
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path

from dotenv_lite import DEFAULT_ENV_FILE, DotenvError, parse_dotenv

REPO_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = REPO_ROOT / "infra" / "compose.yaml"
ENV_EXAMPLE = REPO_ROOT / "infra" / ".env.example"
ENV_FILE = DEFAULT_ENV_FILE
IDENTITY_TEMPLATE = REPO_ROOT / "infra" / "seaweedfs" / "iam.json.tmpl"
IDENTITY_FILE = REPO_ROOT / "infra" / ".generated" / "seaweedfs" / "iam.json"
IDENTITY_FILE_MODE = 0o600
GENERATED_DIR_MODE = 0o700
ENV_FILE_MODE = 0o600

# D-03 calibration: the owner measured 12515225600 bytes of `docker info` MemTotal on the
# Colima vz VM (12 GiB) on 2026-09-30. Rounded down to a multiple of 256 MiB (268435456)
# that is 12348030976. Never lower it to fit a smaller VM.
MIN_VM_BYTES = 12348030976
WAIT_TIMEOUT_S = 300
PROFILE_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")
CANARY_PREFIX = "shpst_"
COLIMA_START = "colima start --vm-type vz --memory 12 --cpu 4"
_TEMPLATE_VARIABLE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_MIN_SIGNING_KEY_BYTES = 16


class PreflightError(RuntimeError):
    """The Docker VM cannot be read or is too small."""


class EnvInitError(RuntimeError):
    """infra/.env or the identity file cannot be produced. The message never holds a value."""


def preflight_verdict(mem_total_bytes: int, threshold_bytes: int) -> tuple[bool, str]:
    """Compare MemTotal with the threshold as integers; GiB figures are for display only."""
    have = f"MemTotal {mem_total_bytes} B ({mem_total_bytes / 2**30:.2f} GiB)"
    need = f"threshold {threshold_bytes} B ({threshold_bytes / 2**30:.2f} GiB)"
    if mem_total_bytes >= threshold_bytes:
        return True, f"VM memory ok: {have}, {need}"
    return False, (
        f"VM memory below threshold: {have}, {need}. "
        f"Start the Colima VM with `{COLIMA_START}` (ADR-001)."
    )


def generate_value(key: str) -> str:
    if key == "CANARY_TOKEN":
        return CANARY_PREFIX + secrets.token_hex(20)
    return secrets.token_hex(32)


def init_env(example_text: str, existing_text: str) -> tuple[str, list[str]]:
    """Return the new file text and the keys filled or added, in example order.

    A fresh file (empty or whitespace only) keeps the example's comments and order with every
    empty value filled. An existing file keeps every non-empty line byte for byte, line ending
    included: an example key it holds with an empty value is filled on its own line, and a key
    it lacks is appended in example order. A key that is not in the example is never touched.
    A duplicate key raises DotenvError.
    """
    example = parse_dotenv(example_text)
    if not existing_text.strip():
        added: list[str] = []
        lines: list[str] = []
        for line in example_text.splitlines():
            key, separator, value = line.partition("=")
            if separator and not line.lstrip().startswith("#") and not value.strip():
                key = key.strip()
                lines.append(f"{key}={generate_value(key)}")
                added.append(key)
            else:
                lines.append(line)
        return "\n".join(lines) + "\n", added
    present = parse_dotenv(existing_text)
    empties = {key for key in example if key in present and not present[key]}
    missing = [key for key in example if key not in present]
    if not empties and not missing:
        return existing_text, []
    kept: list[str] = []
    for raw in existing_text.splitlines(keepends=True):
        body = raw.splitlines()[0]
        key, separator, _ = body.partition("=")
        key = key.strip()
        if separator and not body.strip().startswith("#") and key in empties:
            kept.append(f"{key}={generate_value(key)}{raw[len(body) :]}")
        else:
            kept.append(raw)
    text = "".join(kept)
    if missing:
        text += "" if text.endswith("\n") else "\n"
        text += "".join(f"{key}={example[key] or generate_value(key)}\n" for key in missing)
    return text, [key for key in example if key in empties or key in missing]


def render_identity(template_text: str, env: Mapping[str, str]) -> str:
    """Substitute the template and validate the result. Errors name a variable, never a value."""
    for name in dict.fromkeys(_TEMPLATE_VARIABLE.findall(template_text)):
        if not env.get(name):
            raise EnvInitError(f"identity template variable {name} is missing or empty")
    try:
        rendered = string.Template(template_text).substitute(env)
    except (KeyError, ValueError) as exc:
        raise EnvInitError("identity template could not be substituted") from exc
    try:
        document = json.loads(rendered)
    except ValueError as exc:
        raise EnvInitError("rendered identity file is not valid JSON") from exc
    sts = document.get("sts") if isinstance(document, dict) else None
    signing_key = sts.get("signingKey", "") if isinstance(sts, dict) else ""
    try:
        decoded = base64.b64decode(signing_key, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise EnvInitError("STS_SIGNING_KEY is not valid base64") from exc
    if len(decoded) < _MIN_SIGNING_KEY_BYTES:
        raise EnvInitError(
            f"STS_SIGNING_KEY must decode to at least {_MIN_SIGNING_KEY_BYTES} bytes"
        )
    return rendered


def parse_profiles(env: Mapping[str, str]) -> list[str]:
    """`core` plus the caller's COMPOSE_PROFILES, without `bootstrap` and `*`."""
    profiles = ["core"]
    for raw in env.get("COMPOSE_PROFILES", "").split(","):
        name = raw.strip()
        if not name or name in {"*", "bootstrap"} or name in profiles:
            continue
        if not PROFILE_NAME.match(name):
            raise ValueError(f"invalid compose profile name: {name!r}")
        profiles.append(name)
    return profiles


def compose_argv(profiles: Sequence[str], *args: str) -> list[str]:
    """Compose's --profile flag replaces COMPOSE_PROFILES, so every call passes all of them."""
    argv = ["docker", "compose", "-f", str(COMPOSE_FILE)]
    for name in profiles:
        argv += ["--profile", name]
    return [*argv, *args]


def up_steps(profiles: Sequence[str]) -> list[list[str]]:
    with_bootstrap = [*profiles, "bootstrap"]
    return [
        compose_argv(profiles, "up", "--wait", "--wait-timeout", str(WAIT_TIMEOUT_S)),
        compose_argv(with_bootstrap, "run", "--rm", "warehouse"),
        compose_argv(with_bootstrap, "logs", "--no-log-prefix", "bootstrap"),
    ]


def down_argv(volumes: bool) -> list[str]:
    argv = compose_argv(["*"], "down", "--remove-orphans")
    return [*argv, "--volumes"] if volumes else argv


def child_env(
    base: Mapping[str, str], env_example_keys: Collection[str], env_file_exists: bool
) -> dict[str, str]:
    env = dict(base)
    env["COMPOSE_IGNORE_ORPHANS"] = "true"
    env.pop("COMPOSE_PROFILES", None)
    if not env_file_exists:
        # `down` without infra/.env still interpolates ${NAME:?}; any non-empty value passes.
        for key in env_example_keys:
            env.setdefault(key, "down")
    return env


def run_capture(argv: list[str]) -> tuple[int, str]:
    """Run a command and return (exit code, stdout). Tests replace this."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""
    return result.returncode, result.stdout


def read_mem_total() -> int:
    """MemTotal of the Docker VM in bytes, from `docker info`."""
    code, output = run_capture(["docker", "info", "--format", "{{.MemTotal}}"])
    text = output.strip()
    if code != 0 or not text.isdigit():
        raise PreflightError(
            "could not read the Docker VM memory from `docker info`. Start the Colima VM with "
            f"`{COLIMA_START}` and select the colima docker context (ADR-001)."
        )
    return int(text)


def write_env_fresh(path: Path, text: str) -> None:
    """Create the env file exclusively at mode 0600. Raises FileExistsError if it exists."""
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, ENV_FILE_MODE)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def rewrite_env(path: Path, text: str) -> None:
    """Replace the env file with `text` at mode 0600, via a temp file and os.replace.

    The temp file is in the same directory, so an interrupted run leaves the old file or the
    new one and never a torn file, and a failed replace leaves no temp file behind.
    """
    fd, temp_name = tempfile.mkstemp(dir=path.parent, prefix=".env-", suffix=".tmp")
    try:
        os.fchmod(fd, ENV_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def write_identity(path: Path, text: str) -> None:
    """Write the identity file at 0600 in a 0700 directory, via a temp file and os.replace."""
    directory = path.parent
    directory.mkdir(parents=True, exist_ok=True)
    for private in (directory, *(p for p in directory.parents if p.name == ".generated")):
        private.chmod(GENERATED_DIR_MODE)
    fd, temp_name = tempfile.mkstemp(dir=directory, prefix=".iam-", suffix=".tmp")
    try:
        os.fchmod(fd, IDENTITY_FILE_MODE)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def run_docker(argv: list[str], env: Mapping[str, str]) -> int:
    try:
        return subprocess.run(argv, env=dict(env), cwd=REPO_ROOT, check=False).returncode
    except OSError as exc:
        print(f"could not run docker: {type(exc).__name__}", file=sys.stderr)
        return 127


def _shown(argv: list[str]) -> str:
    return " ".join(argv).replace(f"{REPO_ROOT}/", "")


def sync_env() -> list[str]:
    """Create infra/.env, or top it up, and return the key names filled or added.

    An existing file is made private (0600) on every run, then every empty value is filled
    in place and every absent key appended; a value that is set is never changed. The top-up
    rewrites the whole file atomically, so a whitespace-only file is written whole.
    """
    example = ENV_EXAMPLE.read_text(encoding="utf-8")
    for _attempt in range(2):
        if ENV_FILE.is_file():
            try:
                ENV_FILE.chmod(ENV_FILE_MODE)
            except OSError as exc:
                raise EnvInitError("cannot make the env file private (mode 0600)") from exc
            with ENV_FILE.open(encoding="utf-8", newline="") as handle:
                existing = handle.read()  # newline="" keeps CRLF endings as they are
            text, added = init_env(example, existing)
            if added:
                rewrite_env(ENV_FILE, text)
            return added
        text, added = init_env(example, "")
        try:
            write_env_fresh(ENV_FILE, text)
        except FileExistsError:
            continue  # a concurrent run created it first: top it up instead
        return added
    raise EnvInitError("could not create the env file")


def cmd_up() -> int:
    ok, message = preflight_verdict(read_mem_total(), MIN_VM_BYTES)
    print(message, flush=True)
    if not ok:
        return 1
    added = sync_env()
    if added:
        print(f"env file: added {len(added)} keys: {', '.join(added)}", flush=True)
    else:
        print("env file: up to date", flush=True)
    values = parse_dotenv(ENV_FILE.read_text(encoding="utf-8"))
    write_identity(IDENTITY_FILE, render_identity(IDENTITY_TEMPLATE.read_text("utf-8"), values))
    print("identity file: rendered", flush=True)
    profiles = parse_profiles(os.environ)
    env = child_env(os.environ, values.keys(), env_file_exists=True)
    for argv in up_steps(profiles):
        print(f"+ {_shown(argv)}", flush=True)
        code = run_docker(argv, env)
        if code != 0:
            print(f"step failed with exit code {code}", file=sys.stderr)
            return code
    return 0


def cmd_down(volumes: bool) -> int:
    example_keys = parse_dotenv(ENV_EXAMPLE.read_text(encoding="utf-8")).keys()
    env = child_env(os.environ, example_keys, env_file_exists=ENV_FILE.is_file())
    argv = down_argv(volumes)
    print(f"+ {_shown(argv)}", flush=True)
    return run_docker(argv, env)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Bring the Shopstream stack up or down.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("up", help="bring the core stack up and bootstrap the catalog")
    down = commands.add_parser("down", help="remove every profile's containers")
    down.add_argument("--volumes", action="store_true", help="also delete the named volumes")
    args = parser.parse_args(argv)
    try:
        if args.command == "up":
            return cmd_up()
        return cmd_down(args.volumes)
    except (PreflightError, EnvInitError, DotenvError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
