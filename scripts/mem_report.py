"""Sample and report the stack's memory: `just mem-sample` and `just mem-report`.

`sample` appends one `docker stats` frame every few seconds to .mem/samples.jsonl. `report`
reads those frames and prints the per-service peak table, the peak summed sample, the sum of
the compose `mem_limit` values with and without the Week 5 reserve, the VM headroom verdict
and each container's OOM and restart record. Any breach exits 1; missing samples exit 2.

Counting rule (research P11): every service that has a `mem_limit` in the active profiles
counts toward the limit total, one-shots included. The active profiles are `core` plus
COMPOSE_PROFILES, read with the same rule as `just up`; `bootstrap` is never active here.

The parsers and thresholds are pure and tested without Docker. Nothing here prints the output
of `docker compose config` (it holds interpolated secrets: it is parsed in memory and only
`mem_limit` is read) or the environment of a container (`docker inspect` always runs with
`--format`).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from pathlib import Path
from types import FrameType

from stack import PreflightError, child_env, compose_argv, parse_profiles, read_mem_total

REPO_ROOT = Path(__file__).resolve().parents[1]
SAMPLES_FILE = REPO_ROOT / ".mem" / "samples.jsonl"
SAMPLE_INTERVAL_S = 5
PEAK_BUDGET_BYTES = 10 * 1024**3  # INV-20: the peak summed sample stays within 10 GiB
VM_HEADROOM_BYTES = 1024**3  # sum(mem_limit) stays within MemTotal minus 1 GiB
W05_RESERVE_BYTES = 384 * 1024**2  # informational: the Week 5 streaming reserve
PROJECT = "shopstream"
DOCKER_TIMEOUT_S = 120

# docker stats prints binary units as KiB/MiB/GiB and decimal ones as kB/MB/GB.
UNITS: dict[str, int] = {
    "B": 1,
    "KiB": 1024,
    "MiB": 1024**2,
    "GiB": 1024**3,
    "TiB": 1024**4,
    "kB": 1000,
    "MB": 1000**2,
    "GB": 1000**3,
}
_SIZE = re.compile(r"^\s*(?P<number>\d+(?:\.\d+)?)\s*(?P<unit>[A-Za-z]+)\s*$")
_ABSENT = "--"
CAVEAT = (
    "caveat: docker stats prints four significant digits and samples every "
    f"{SAMPLE_INTERVAL_S} s, so a short peak can fall between samples; the OOM and restart "
    "checks cover it."
)


class DockerError(RuntimeError):
    """A Docker call failed. The message names the exit code, never Docker's output."""


@dataclass(frozen=True)
class Frame:
    ts: str
    usage: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.usage.values())


# --- pure parsers and thresholds ---------------------------------------------------------


def parse_size(text: str) -> int:
    """Whole bytes in a docker size such as `2.352MiB`. An unknown unit raises ValueError."""
    match = _SIZE.match(text)
    if match is None:
        raise ValueError(f"not a size: {text!r}")
    unit = UNITS.get(match.group("unit"))
    if unit is None:
        raise ValueError(f"unknown size unit: {match.group('unit')!r}")
    try:
        exact = Decimal(match.group("number")) * unit
    except InvalidOperation as exc:  # unreachable behind the regex; kept for safety
        raise ValueError(f"not a size: {text!r}") from exc
    return int(exact.quantize(Decimal(1), rounding=ROUND_HALF_EVEN))


def parse_mem_usage(text: str) -> tuple[int, int] | None:
    """(used, limit) in bytes from `2.352MiB / 982.5MiB`; None for a stopped `-- / --`."""
    used_text, separator, limit_text = text.partition(" / ")
    if not separator:
        raise ValueError(f"not a MemUsage value: {text!r}")
    if used_text.strip() == _ABSENT or limit_text.strip() == _ABSENT:
        return None
    return parse_size(used_text), parse_size(limit_text)


def service_of(name: str, project: str = PROJECT) -> str:
    """`shopstream-lakekeeper-migrate-1` and `/shopstream-postgres-1` to the service name."""
    return re.sub(rf"^{re.escape(project)}-|-\d+$", "", name.lstrip("/"))


def load_frames(path: Path) -> list[Frame]:
    """Frames from a samples file; a missing or empty file gives none.

    A row showing `-- / --` is left out of its frame, never counted as zero. A line that is
    not valid JSON (a write cut off by Ctrl-C) is skipped.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    frames: list[Frame] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            ts = str(record["ts"])
            rows = record["rows"]
            usage: dict[str, int] = {}
            for row in rows:
                parsed = parse_mem_usage(str(row["mem_usage"]))
                if parsed is None:
                    continue
                service = service_of(str(row["name"]))
                usage[service] = usage.get(service, 0) + parsed[0]
        except (ValueError, KeyError, TypeError):
            continue
        frames.append(Frame(ts=ts, usage=usage))
    return frames


def service_peaks(frames: Sequence[Frame]) -> list[tuple[str, int]]:
    """Per-service peak bytes, largest first, ties by service name."""
    peaks: dict[str, int] = {}
    for frame in frames:
        for service, used in frame.usage.items():
            peaks[service] = max(peaks.get(service, 0), used)
    return sorted(peaks.items(), key=lambda item: (-item[1], item[0]))


def peak_frame(frames: Sequence[Frame]) -> tuple[str, int]:
    """(timestamp, summed bytes) of the largest frame; the earliest one when frames tie."""
    if not frames:
        raise ValueError("no frames")
    best = min(frames, key=lambda frame: (-frame.total, frame.ts))
    return best.ts, best.total


def limits_by_service(config: Mapping[str, object]) -> tuple[dict[str, int], list[str]]:
    """(mem_limit bytes per service, services with no usable mem_limit)."""
    services = config.get("services")
    limits: dict[str, int] = {}
    missing: list[str] = []
    if not isinstance(services, Mapping):
        return limits, missing
    for name in sorted(services):
        definition = services[name]
        raw = definition.get("mem_limit") if isinstance(definition, Mapping) else None
        if isinstance(raw, bool) or raw is None:
            missing.append(str(name))
        elif isinstance(raw, int):
            limits[str(name)] = raw
        elif isinstance(raw, str) and raw.isdigit():
            limits[str(name)] = int(raw)
        else:
            missing.append(str(name))
    return limits, missing


def mem_limit_total(config: Mapping[str, object]) -> tuple[int, list[str]]:
    """(sum of mem_limit bytes over the services in `config`, services without one)."""
    limits, missing = limits_by_service(config)
    return sum(limits.values()), missing


def parse_inspect(text: str) -> list[tuple[str, bool, int]]:
    """(service, OOMKilled, RestartCount) per line of the fixed `docker inspect` format."""
    rows: list[tuple[str, bool, int]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if len(parts) != 3 or parts[1] not in {"true", "false"} or not parts[2].isdigit():
            raise ValueError("unexpected docker inspect line")
        rows.append((service_of(parts[0]), parts[1] == "true", int(parts[2])))
    return rows


def evaluate(
    peak_sum: int,
    limit_total: int,
    missing: Sequence[str],
    mem_total: int,
    inspect_rows: Sequence[tuple[str, bool, int]],
) -> list[str]:
    """Breach messages; an empty list means every threshold holds."""
    breaches: list[str] = []
    if peak_sum > PEAK_BUDGET_BYTES:
        breaches.append(
            f"peak summed sample {peak_sum} B is over the {PEAK_BUDGET_BYTES} B (10 GiB) budget"
        )
    ceiling = mem_total - VM_HEADROOM_BYTES
    if limit_total > ceiling:
        breaches.append(
            f"sum(mem_limit) {limit_total} B is over MemTotal minus 1 GiB ({ceiling} B)"
        )
    breaches.extend(f"service {name} has no mem_limit" for name in missing)
    for service, oom_killed, restarts in inspect_rows:
        if oom_killed:
            breaches.append(f"container {service} was OOM killed")
        if restarts > 0:
            breaches.append(f"container {service} restarted {restarts} time(s)")
    return breaches


# --- Docker I/O (tests replace these) ------------------------------------------------------


def _docker(argv: list[str]) -> str:
    env = child_env(os.environ, [], env_file_exists=True)
    try:
        result = subprocess.run(
            argv,
            env=env,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=DOCKER_TIMEOUT_S,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerError(f"could not run {argv[1]}: {type(exc).__name__}") from exc
    if result.returncode != 0:
        raise DockerError(f"`docker {argv[1]}` failed with exit code {result.returncode}")
    return result.stdout


def docker_stats_frame() -> list[dict[str, str]]:
    """One `docker stats` reading: Name and MemUsage of every shopstream container."""
    output = _docker(["docker", "stats", "--no-stream", "--format", "{{json .}}"])
    rows: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        name = str(record.get("Name", ""))
        if name.startswith(f"{PROJECT}-"):
            rows.append({"name": name, "mem_usage": str(record.get("MemUsage", ""))})
    return rows


def compose_config(profiles: Sequence[str]) -> dict[str, object]:
    """The resolved compose model, parsed in memory. It is never printed."""
    output = _docker(compose_argv(profiles, "config", "--format", "json"))
    config = json.loads(output)
    if not isinstance(config, dict):
        raise DockerError("`docker compose config` did not return a JSON object")
    return config


def inspect_rows(profiles: Sequence[str]) -> list[tuple[str, bool, int]]:
    """OOMKilled and RestartCount of every container, one-shots included."""
    listing = _docker(compose_argv([*profiles, "bootstrap"], "ps", "-aq"))
    container_ids = [line.strip() for line in listing.splitlines() if line.strip()]
    if not container_ids:
        return []
    fmt = "{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}"
    return parse_inspect(_docker(["docker", "inspect", "--format", fmt, *container_ids]))


# --- commands ------------------------------------------------------------------------------


def _gib(value: int) -> str:
    return f"{value / 1024**3:.2f} GiB"


def _mib(value: int) -> str:
    return f"{value / 1024**2:.1f}"


def cmd_sample(duration: float | None, interval: float, append: bool, path: Path) -> int:
    stop = threading.Event()

    def on_term(_signum: int, _frame: FrameType | None) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, on_term)
    path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    count = 0
    try:
        with path.open("a" if append else "w", encoding="utf-8") as handle:
            while not stop.is_set():
                arrived = time.monotonic()
                if duration is not None and arrived - started >= duration:
                    break
                stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
                record = {"ts": stamp, "rows": docker_stats_frame()}
                handle.write(json.dumps(record) + "\n")
                handle.flush()
                count += 1
                stop.wait(max(0.0, interval - (time.monotonic() - arrived)))
    except KeyboardInterrupt:
        pass
    print(f"mem-sample: {count} frames written to {path.name}")
    return 0


def cmd_report(samples: Path) -> int:
    frames = load_frames(samples)
    if not frames:
        print(f"mem-report: no samples in {samples.name}; run just mem-sample first")
        return 2
    profiles = parse_profiles(os.environ)
    config = compose_config(profiles)
    limits, missing = limits_by_service(config)
    limit_total = sum(limits.values())
    mem_total = read_mem_total()
    rows = inspect_rows(profiles)

    print(f"mem-report: {len(frames)} frames, {frames[0].ts} to {frames[-1].ts}")
    print(f"{'service':<22}{'peak MiB':>10}{'mem_limit MiB':>15}")
    for service, peak in service_peaks(frames):
        limit = _mib(limits[service]) if service in limits else "-"
        print(f"{service:<22}{_mib(peak):>10}{limit:>15}")
    peak_ts, peak_sum = peak_frame(frames)
    print(f"peak summed sample: {_gib(peak_sum)} at {peak_ts} (budget {_gib(PEAK_BUDGET_BYTES)})")
    listed = ", ".join(profiles)
    print(f"sum(mem_limit) for profiles {listed}: {_gib(limit_total)}")
    print(f"sum(mem_limit) with the 384 MiB W05 reserve: {_gib(limit_total + W05_RESERVE_BYTES)}")
    ceiling = mem_total - VM_HEADROOM_BYTES
    verdict = "ok" if limit_total <= ceiling else "over"
    print(f"VM MemTotal {_gib(mem_total)}, minus 1 GiB leaves {_gib(ceiling)}: limits {verdict}")
    print("OOMKilled and RestartCount per container:")
    for service, oom_killed, restarts in rows:
        print(f"  {service}: OOMKilled={str(oom_killed).lower()} RestartCount={restarts}")
    if not rows:
        print("  no containers found")
    print(CAVEAT)

    breaches = evaluate(peak_sum, limit_total, missing, mem_total, rows)
    for message in breaches:
        print(f"BREACH: {message}")
    if breaches:
        return 1
    print("mem-report: ok")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample and report the stack's memory.")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="append docker stats frames to the samples file")
    sample.add_argument("--duration", type=float, help="stop after this many seconds")
    sample.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_S)
    sample.add_argument("--append", action="store_true", help="keep the existing samples")
    report = commands.add_parser("report", help="print peaks, limit totals and the OOM check")
    report.add_argument("--samples", type=Path, default=SAMPLES_FILE)
    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            return cmd_sample(args.duration, args.interval, args.append, SAMPLES_FILE)
        return cmd_report(args.samples)
    except (DockerError, PreflightError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
