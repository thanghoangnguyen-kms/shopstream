"""Sample and report the stack's memory: `just mem-sample` and `just mem-report`.

`sample` appends one `docker stats` frame every few seconds to .mem/samples.jsonl. `report`
reads those frames and prints the per-service peak table, the peak summed sample, the sum of
the compose `mem_limit` values with and without the Week 5 reserve, the VM headroom verdict
and each container's OOM, restart and state record. Checks: peak over 10 GiB, limit total,
missing mem_limit, OOM kill, restart, exit code 137, long-running service not running or with
no container, long-running service never sampled, and a peak summed sample of 0 bytes.
Exit codes: 0 ok, 1 breach, 2 no usable samples (none, or no frame with container rows) or
fewer frames with container rows than `--min-frames N` (default 1).

Two `docker inspect` calls, each with an explicit `--format`: INSPECT_FORMAT
(`{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}`, the PLAT-09 literal) and STATE_FORMAT
(`{{.Name}} {{.State.Status}} {{.State.ExitCode}}`). The state check is what catches a container
killed outside its cgroup limit (a VM-level OOM kill or a crash), which leaves OOMKilled false
and, with `restart: "no"` on every service, RestartCount 0: a long-running service that is not
`running`, a long-running service with no container, or any container that exited 137 breaches.
Long-running services follow the repo policy test's one-shot rule.

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
from collections.abc import Collection, Mapping, Sequence
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
SPIKE_PROFILE = "spike"
DOCKER_TIMEOUT_S = 120
INSPECT_FORMAT = "{{.Name}} {{.State.OOMKilled}} {{.RestartCount}}"  # PLAT-09, kept verbatim
STATE_FORMAT = "{{.Name}} {{.State.Status}} {{.State.ExitCode}}"
DOCKER_STATES = frozenset(
    {"created", "running", "paused", "restarting", "removing", "exited", "dead"}
)
OOM_KILL_EXIT_CODE = 137  # 128 + SIGKILL, what the out-of-memory killer sends
EVENTS_FORMAT = (
    '{{.Time}} {{.Action}} {{.Actor.Attributes.name}} {{with index .Actor.Attributes "exitCode"}}'
    "{{.}}{{else}}-{{end}}"
)
EVENT_ACTIONS = frozenset({"oom", "die"})

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
_EXIT_CODE = re.compile(r"-?[0-9]+")
CAVEAT = (
    "caveat: docker stats prints four significant digits and samples every "
    f"{SAMPLE_INTERVAL_S} s, so a short peak can fall between samples; a peak that kills a "
    "container still shows as OOMKilled, exit code 137 or a long-running service that is not "
    'running; every service has restart "no", so RestartCount stays 0 unless a restart policy '
    "is added."
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
    """`shopstream-lakekeeper-migrate-1` and `/shopstream-postgres-1` to the service name.

    A `compose run` container (`shopstream-spark-job-run-4f87af04bf9d`, and for the service named
    cdc-run `shopstream-cdc-run-run-0123456789ab`) maps to its service, so the table shows its limit.
    """
    return re.sub(rf"^{re.escape(project)}-|-run-[0-9a-f]{{12}}$|-\d+$", "", name.lstrip("/"))


def events_argv(since: str) -> list[str]:
    """The fixed `docker events` argv for the oom and die events of the project's containers.

    It streams until it is stopped. Every call carries an explicit --format, never the default
    JSON, so an event's attributes (an environment-derived label, say) never reach the capture.
    """
    return [
        "docker",
        "events",
        "--since",
        since,
        "--filter",
        f"label=com.docker.compose.project={PROJECT}",
        "--filter",
        "event=oom",
        "--filter",
        "event=die",
        "--format",
        EVENTS_FORMAT,
    ]


def parse_events(text: str) -> list[tuple[str, str, int | None]]:
    """(service, action, exit code or None) per line of EVENTS_FORMAT, in capture order.

    Anything that is not `<epoch seconds> oom|die <name> <exit code or ->` raises ValueError
    without echoing the line.
    """
    rows: list[tuple[str, str, int | None]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if (
            len(parts) != 4
            or not parts[0].isdigit()
            or parts[1] not in EVENT_ACTIONS
            or (parts[3] != "-" and _EXIT_CODE.fullmatch(parts[3]) is None)
        ):
            raise ValueError("unexpected docker events line")
        rows.append((service_of(parts[2]), parts[1], None if parts[3] == "-" else int(parts[3])))
    return rows


def read_samples(path: Path) -> tuple[list[Frame], int]:
    """(frames, skipped lines) from a samples file; a missing or empty file gives none.

    A row showing `-- / --` is left out of its frame, never counted as zero, so a frame can
    hold no rows at all. A non-blank line that does not parse (a write cut off by Ctrl-C, a
    row with an unreadable MemUsage) is skipped and counted.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], 0
    frames: list[Frame] = []
    skipped = 0
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
            skipped += 1
            continue
        frames.append(Frame(ts=ts, usage=usage))
    return frames, skipped


def load_frames(path: Path) -> list[Frame]:
    """Frames from a samples file; a missing or empty file gives none."""
    return read_samples(path)[0]


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


def parse_state(text: str) -> list[tuple[str, str, int]]:
    """(service, status, exit code) per line of the fixed `docker inspect` state format."""
    rows: list[tuple[str, str, int]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        parts = line.split()
        if (
            len(parts) != 3
            or parts[1] not in DOCKER_STATES
            or _EXIT_CODE.fullmatch(parts[2]) is None
        ):
            raise ValueError("unexpected docker inspect state line")
        rows.append((service_of(parts[0]), parts[1], int(parts[2])))
    return rows


def long_running_services(config: Mapping[str, object]) -> list[str]:
    """Services that must stay up: every one except the one-shots, sorted.

    Same rule as the repo policy test: a service in the `bootstrap` profile, or one another
    service waits on with `service_completed_successfully`, is a one-shot.
    """
    services = config.get("services")
    if not isinstance(services, Mapping):
        return []
    one_shots: set[str] = set()
    for name, definition in services.items():
        if not isinstance(definition, Mapping):
            continue
        profiles = definition.get("profiles")
        if isinstance(profiles, list) and "bootstrap" in profiles:
            one_shots.add(str(name))
        depends = definition.get("depends_on")
        if isinstance(depends, Mapping):
            one_shots.update(
                str(dep)
                for dep, condition in depends.items()
                if isinstance(condition, Mapping)
                and condition.get("condition") == "service_completed_successfully"
            )
    return sorted(str(name) for name in services if str(name) not in one_shots)


def coverage_breaches(frames: Sequence[Frame], long_running: Collection[str]) -> list[str]:
    """Breaches that say the samples do not cover the stack: a long-running service that
    appears in no frame, then a peak summed sample of 0 bytes."""
    sampled = {service for frame in frames for service in frame.usage}
    breaches = [
        f"service {service} was never sampled" for service in sorted(set(long_running) - sampled)
    ]
    if frames and peak_frame(frames)[1] == 0:
        breaches.append("peak summed sample is 0 B; no memory use was observed")
    return breaches


def evaluate(
    peak_sum: int,
    limit_total: int,
    missing: Sequence[str],
    mem_total: int,
    inspect_rows: Sequence[tuple[str, bool, int]],
    *,
    states: Sequence[tuple[str, str, int]] = (),
    long_running: Collection[str] = (),
    events: Sequence[tuple[str, str, int | None]] = (),
) -> list[str]:
    """Breach messages; an empty list means every threshold holds.

    `events` is a `docker events` capture: each oom event, and each die with exit code 137, is a
    breach even for a container `--rm` has already removed.
    """
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
    for service, status, exit_code in states:
        if exit_code == OOM_KILL_EXIT_CODE and status != "running":
            breaches.append(
                f"container {service} exited with code {OOM_KILL_EXIT_CODE} "
                "(SIGKILL, which the out-of-memory killer sends)"
            )
        if service in long_running and status != "running":
            breaches.append(
                f"long-running container {service} is {status} (exit code {exit_code}), not running"
            )
    with_container = {service for service, _status, _code in states}
    breaches.extend(
        f"long-running service {service} has no container"
        for service in sorted(set(long_running) - with_container)
    )
    for service, action, event_code in events:
        if action == "oom":
            breaches.append(f"container {service} had an OOM event")
        elif action == "die" and event_code == OOM_KILL_EXIT_CODE:
            breaches.append(
                f"container {service} exited with code {OOM_KILL_EXIT_CODE} in the events capture "
                "(SIGKILL, which the out-of-memory killer sends)"
            )
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


def container_ids(profiles: Sequence[str]) -> list[str]:
    """Ids of every container of the project, one-shots and spike one-offs included.

    `bootstrap` and `spike` are added so a `compose run` container that still exists (a Spark job
    run without `--rm`) is inspected too.
    """
    listing = _docker(compose_argv([*profiles, "bootstrap", "spike"], "ps", "-aq"))
    return [line.strip() for line in listing.splitlines() if line.strip()]


def inspect_rows(profiles: Sequence[str]) -> list[tuple[str, bool, int]]:
    """OOMKilled and RestartCount of every container, one-shots included."""
    ids = container_ids(profiles)
    if not ids:
        return []
    return parse_inspect(_docker(["docker", "inspect", "--format", INSPECT_FORMAT, *ids]))


def state_rows(profiles: Sequence[str]) -> list[tuple[str, str, int]]:
    """Status and exit code of every container, one-shots included."""
    ids = container_ids(profiles)
    if not ids:
        return []
    return parse_state(_docker(["docker", "inspect", "--format", STATE_FORMAT, *ids]))


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


def _report_json(
    *,
    frames: Sequence[Frame],
    peaks: Sequence[tuple[str, int]],
    peak_ts: str,
    peak_sum: int,
    all_limits: Mapping[str, int],
    limit_total_profiles: int,
    limit_total: int,
    with_services: Sequence[str],
    mem_total: int,
    ceiling: int,
    rows: Sequence[tuple[str, bool, int]],
    states: Sequence[tuple[str, str, int]],
    events: Sequence[tuple[str, str, int | None]],
    breaches: Sequence[str],
    long_running: Sequence[str],
    missing: Sequence[str],
) -> dict[str, object]:
    """Every figure of the report as plain JSON values, in a fixed order."""
    return {
        "frames": len(frames),
        "peak_sum": peak_sum,
        "peak_ts": peak_ts,
        "per_service": [
            {"service": service, "peak_bytes": peak, "limit_bytes": all_limits.get(service)}
            for service, peak in peaks
        ],
        "limit_total_profiles": limit_total_profiles,
        "limit_total": limit_total,
        "with_services": list(with_services),
        "mem_total": mem_total,
        "ceiling": ceiling,
        "inspect": [
            {"service": service, "oom_killed": oom_killed, "restarts": restarts}
            for service, oom_killed, restarts in sorted(rows)
        ],
        "states": [
            {"service": service, "status": status, "exit_code": code}
            for service, status, code in sorted(states)
        ],
        "events": [
            {"service": service, "action": action, "exit_code": code}
            for service, action, code in events
        ],
        "breaches": list(breaches),
        "long_running": list(long_running),
        "missing_limits": list(missing),
    }


def cmd_report(
    samples: Path,
    min_frames: int = 1,
    with_services: Sequence[str] = (),
    events_path: Path | None = None,
    json_out: Path | None = None,
) -> int:
    all_frames, skipped = read_samples(samples)
    if not all_frames:
        print(f"mem-report: no samples in {samples.name}; run just mem-sample first")
        return 2
    frames = [frame for frame in all_frames if frame.usage]
    if not frames:
        print(
            f"mem-report: no container rows in {samples.name}; "
            "was the stack running while sampling?"
        )
        return 2
    if len(frames) < min_frames:
        print(
            f"mem-report: {len(frames)} frames with container rows in {samples.name}, "
            f"fewer than --min-frames {min_frames}"
        )
        return 2
    events = parse_events(events_path.read_text(encoding="utf-8")) if events_path else []
    profiles = parse_profiles(os.environ)
    config = compose_config(profiles)
    # The active profiles give the totals and the long-running set; adding `spike` gives the limit
    # of a service only an explicit `run` starts (spark-job), for the table and --with-service.
    all_limits = limits_by_service(compose_config([*profiles, SPIKE_PROFILE]))[0]
    limits, missing = limits_by_service(config)
    extra = list(dict.fromkeys(with_services))
    unusable = [name for name in extra if name not in all_limits or name in limits]
    if unusable:
        print(
            f"mem-report: --with-service {', '.join(unusable)}: not a spike-only service with a "
            "mem_limit (unknown, already in the active profiles, or without a limit)"
        )
        return 2
    limit_total_profiles = sum(limits.values())
    limit_total = limit_total_profiles + sum(all_limits[name] for name in extra)
    mem_total = read_mem_total()
    long_running = long_running_services(config)
    rows = inspect_rows(profiles)
    states = state_rows(profiles)
    state_of = {service: (status, code) for service, status, code in states}

    print(
        f"mem-report: {len(frames)} frames with container rows, {frames[0].ts} to "
        f"{frames[-1].ts}; {skipped} lines skipped, {len(all_frames) - len(frames)} frames "
        "without container rows"
    )
    print(f"{'service':<22}{'peak MiB':>10}{'mem_limit MiB':>15}")
    peaks = service_peaks(frames)
    for service, peak in peaks:
        limit = _mib(all_limits[service]) if service in all_limits else "-"
        print(f"{service:<22}{_mib(peak):>10}{limit:>15}")
    peak_ts, peak_sum = peak_frame(frames)
    print(f"peak summed sample: {_gib(peak_sum)} at {peak_ts} (budget {_gib(PEAK_BUDGET_BYTES)})")
    listed = ", ".join(profiles)
    print(f"sum(mem_limit) for profiles {listed}: {_gib(limit_total_profiles)}")
    if extra:
        print(f"sum(mem_limit) with {', '.join(extra)}: {_gib(limit_total)}")
    print(f"sum(mem_limit) with the 384 MiB W05 reserve: {_gib(limit_total + W05_RESERVE_BYTES)}")
    ceiling = mem_total - VM_HEADROOM_BYTES
    verdict = "ok" if limit_total <= ceiling else "over"
    print(f"VM MemTotal {_gib(mem_total)}, minus 1 GiB leaves {_gib(ceiling)}: limits {verdict}")
    print("OOMKilled and RestartCount per container:")
    for service, oom_killed, restarts in rows:
        status, code = state_of.get(service, ("-", "-"))
        print(
            f"  {service}: OOMKilled={str(oom_killed).lower()} RestartCount={restarts} "
            f"Status={status} ExitCode={code}"
        )
    if not rows:
        print("  no containers found")
    if events_path is not None:
        print(f"events capture {events_path.name}: {len(events)} oom or die events")
    print(CAVEAT)

    breaches = evaluate(
        peak_sum,
        limit_total,
        missing,
        mem_total,
        rows,
        states=states,
        long_running=long_running,
        events=events,
    )
    breaches.extend(coverage_breaches(frames, long_running))
    for message in breaches:
        print(f"BREACH: {message}")
    if json_out is not None:
        body = _report_json(
            frames=frames,
            peaks=peaks,
            peak_ts=peak_ts,
            peak_sum=peak_sum,
            all_limits=all_limits,
            limit_total_profiles=limit_total_profiles,
            limit_total=limit_total,
            with_services=extra,
            mem_total=mem_total,
            ceiling=ceiling,
            rows=rows,
            states=states,
            events=events,
            breaches=breaches,
            long_running=long_running,
            missing=missing,
        )
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if breaches:
        return 1
    print("mem-report: ok")
    return 0


def _positive_int(text: str) -> int:
    if re.fullmatch(r"[0-9]+", text) is None or int(text) < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return int(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Sample and report the stack's memory.")
    commands = parser.add_subparsers(dest="command", required=True)
    sample = commands.add_parser("sample", help="append docker stats frames to the samples file")
    sample.add_argument("--duration", type=float, help="stop after this many seconds")
    sample.add_argument("--interval", type=float, default=SAMPLE_INTERVAL_S)
    sample.add_argument("--append", action="store_true", help="keep the existing samples")
    sample.add_argument(
        "--out", type=Path, help="write the samples here (default .mem/samples.jsonl)"
    )
    report = commands.add_parser("report", help="print peaks, limit totals and the OOM check")
    report.add_argument("--samples", type=Path, default=SAMPLES_FILE)
    report.add_argument(
        "--min-frames",
        type=_positive_int,
        default=1,
        help="exit 2 when fewer frames than this hold container rows",
    )
    report.add_argument(
        "--with-service",
        action="append",
        default=[],
        metavar="NAME",
        help="add this spike service's mem_limit to the total (repeatable)",
    )
    report.add_argument("--events", type=Path, help="a `docker events` capture to check for OOM")
    report.add_argument("--json-out", type=Path, help="write every figure to this JSON file")
    args = parser.parse_args(argv)
    try:
        if args.command == "sample":
            return cmd_sample(args.duration, args.interval, args.append, args.out or SAMPLES_FILE)
        return cmd_report(
            args.samples, args.min_frames, args.with_service, args.events, args.json_out
        )
    except (DockerError, PreflightError, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
