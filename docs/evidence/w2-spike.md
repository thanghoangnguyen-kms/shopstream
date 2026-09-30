# Week 2 spike evidence

Recorded from 2026-09-30.

Every capture passes through `scripts/redact_evidence.py` before it is written here, and is trimmed to about 40 lines. Each Item section gives the pinned versions, the exact command, the trimmed output, the date and a verdict line once its item runs. Evidence records counts and hashes, never generated PII values or the erasure canary.

## Platform base

Recorded 2026-09-30, from a fresh `git clone` of the phase branch with no named volumes, no `infra/.env` and no `infra/.generated/`. Every capture below went to a temporary file outside both trees first and passed through `scripts/redact_evidence.py` after `infra/.env` existed, so Layer 2 masked the freshly generated values.

### Runtime

The container runtime is Colima on the Apple Virtualization framework: a `vz` VM with virtiofs mounts, 12 GiB of memory and 4 CPUs, started with `colima start --vm-type vz --memory 12 --cpu 4`. The VM disk is 40 GiB; ADR-001 go criterion 12 later projects 75 percent of that disk (30 GiB) against the Iceberg data footprint. `docker info` reports MemTotal 12515225600 B, and `just up` refuses a VM under `MIN_VM_BYTES` 12348030976 B (D-03: the measured MemTotal rounded down to a multiple of 256 MiB).

```text
$ docker context show
colima
$ colima version
colima version 0.10.3
git commit: 00f6c297e92a82c04a4ab507db0a61435650d7e8

runtime: docker
arch: aarch64
client: v29.6.2
server: v29.5.2
$ colima list
PROFILE    STATUS     ARCH       CPUS    MEMORY    DISK     RUNTIME    ADDRESS
default    Running    aarch64    4       12GiB     40GiB    docker
$ colima status --json (driver, mount_type)
{'driver': 'macOS Virtualization.Framework', 'mount_type': 'virtiofs'}
$ docker version --format '{{.Client.Version}} {{.Server.Version}}'
29.6.2 29.5.2
$ docker compose version
Docker Compose version 5.3.1
$ docker info --format '{{.OperatingSystem}} {{.MemTotal}}'
Ubuntu 24.04.4 LTS 12515225600
$ grep -n "^MIN_VM_BYTES" scripts/stack.py
43:MIN_VM_BYTES = 12348030976
```

### Clean-clone just up

`uv run just up` from the fresh clone. The Compose `Creating`, `Created`, `Starting`, `Waiting` and `Running` state lines are dropped, so the `Started`, `Healthy` and `Exited` lines remain. The transcript ends with the exit code.

```text
uv run python scripts/stack.py up
VM memory ok: MemTotal 12515225600 B (11.66 GiB), threshold 12348030976 B (11.50 GiB)
env file: added 13 keys: POSTGRES_PASSWORD, LAKEKEEPER_DB_PASSWORD, AIRFLOW_DB_PASSWORD, SHOPSTREAM_DB_PASSWORD, LAKEKEEPER_PG_ENCRYPTION_KEY, SEAWEEDFS_ADMIN_KEY, SEAWEEDFS_ADMIN_SECRET, LAKEKEEPER_S3_KEY, LAKEKEEPER_S3_SECRET, PROBE_OTHER_KEY, PROBE_OTHER_SECRET, STS_SIGNING_KEY, CANARY_TOKEN
identity file: rendered
+ docker compose -f infra/compose.yaml --profile core up --wait --wait-timeout 300
 Container shopstream-seaweedfs-1 Started
 Container shopstream-postgres-1 Started
 Container shopstream-frankfurter-init-1 Started
 Container shopstream-frankfurter-init-1 Exited
 Container shopstream-frankfurter-1 Started
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-migrate-1 Started
 Container shopstream-lakekeeper-migrate-1 Exited
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-1 Started
 Container shopstream-frankfurter-init-1 Exited
 Container shopstream-frankfurter-1 Healthy
 Container shopstream-lakekeeper-migrate-1 Exited
 Container shopstream-postgres-1 Healthy
 Container shopstream-seaweedfs-1 Healthy
 Container shopstream-lakekeeper-1 Healthy
+ docker compose -f infra/compose.yaml --profile core --profile bootstrap run --rm warehouse
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-migrate-1 Started
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-migrate-1 Exited
 Container shopstream-lakekeeper-1 Healthy
 Container shopstream-seaweedfs-1 Healthy
 Container shopstream-bootstrap-1 Started
 Container shopstream-seaweedfs-1 Healthy
 Container shopstream-lakekeeper-1 Healthy
 Container shopstream-bootstrap-1 Exited
warehouse spike: created
+ docker compose -f infra/compose.yaml --profile core --profile bootstrap logs --no-log-prefix bootstrap
bucket warehouse: created
catalog bootstrap: created
exit=0
```

### Services

```text
$ docker compose -f infra/compose.yaml --profile core ps --format 'table {{.Service}}	{{.Status}}'
SERVICE       STATUS
frankfurter   Up About a minute (healthy)
lakekeeper    Up 58 seconds (healthy)
postgres      Up About a minute (healthy)
seaweedfs     Up About a minute (healthy)
```

### Postgres

`wal_level`, `track_commit_timestamp`, then `relreplident` per captured table (`f` is `REPLICA IDENTITY FULL`).

```text
$ docker compose -f infra/compose.yaml --profile core exec -T postgres psql -U postgres -d shopstream -Atc "select current_setting('wal_level') || ',' || current_setting('track_commit_timestamp') || ',' || string_agg(relname || '=' || relreplident::text, ',' order by relname) from pg_class where relname in ('customers','products','orders','order_items') and relkind = 'r'"
logical,on,customers=f,order_items=f,orders=f,products=f
```

### Identities

The identity and trust-policy view of the rendered SeaweedFS file, without any key or secret, then the mode of that file and of the env file. The `lakekeeper` identity carries no `sts:AssumeRole` action; the role's trust policy names only `arn:aws:iam::000000000000:user/lakekeeper` (see Deviations).

```text
$ python identities view (names, actions, trust policy, attached policy; no credentials) and file modes
identity admin ['Admin', 'Read', 'List', 'Tagging', 'Write']
identity lakekeeper ['Read', 'List', 'Tagging', 'Write']
identity probe-other ['Read', 'List']
sts {'tokenDuration': '1h', 'maxSessionLength': '1h', 'issuer': 'seaweedfs-sts', 'accountId': '000000000000'}
role LakekeeperVendedRole attached ['WarehouseBucket']
trustPolicy {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::000000000000:user/lakekeeper"}, "Action": "sts:AssumeRole"}]}
policy WarehouseBucket {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:*", "Resource": ["arn:aws:s3:::warehouse", "arn:aws:s3:::warehouse/*"]}]}
600 infra/.generated/seaweedfs/iam.json
600 infra/.env
```

### Memory limits

The figures in this section are from the clean-clone run at Frankfurter's 416m limit; the final limit is 640m (Deviations, item 5).

`HostConfig.Memory` and `HostConfig.MemorySwap` are equal for every `core` service, so no container can use swap: frankfurter 416 MiB, lakekeeper 256 MiB, postgres 512 MiB, seaweedfs 768 MiB.

```text
$ docker inspect --format '{{.Name}} {{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}' $(docker compose -f infra/compose.yaml --profile core ps -q)
/shopstream-frankfurter-1 436207616 436207616
/shopstream-lakekeeper-1 268435456 268435456
/shopstream-postgres-1 536870912 536870912
/shopstream-seaweedfs-1 805306368 805306368
```

### Idempotent one-shots

A second `warehouse` run against the same volumes.

```text
$ docker compose -f infra/compose.yaml --profile core --profile bootstrap run --rm warehouse
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-migrate-1 Started
 Container shopstream-lakekeeper-migrate-1 Exited
 Container shopstream-postgres-1 Healthy
 Container shopstream-lakekeeper-1 Healthy
 Container shopstream-seaweedfs-1 Healthy
 Container shopstream-bootstrap-1 Started
 Container shopstream-lakekeeper-1 Healthy
 Container shopstream-seaweedfs-1 Healthy
 Container shopstream-bootstrap-1 Exited
warehouse spike: exists
exit=0
```

### mem-report

`uv run just mem-sample --duration 2100` ran in the clone from the first minute of the backfill (420 frames, one every 5 s, 35 minutes), then `uv run just mem-report` read the samples while the stack was still up. This is longer than the 60 s the plan names, because Frankfurter's first provider backfill runs for over half an hour and the limit has to hold for all of it. Docker stats prints four significant digits and samples every 5 s, so a short peak can be missed; the OOM and restart checks cover it.

```text
uv run python scripts/mem_report.py report
mem-report: 420 frames, 2026-09-30T06:36:53Z to 2026-09-30T07:11:50Z
service                 peak MiB  mem_limit MiB
frankfurter                412.4          416.0
seaweedfs                   98.3          768.0
postgres                    62.0          512.0
lakekeeper                  23.8          256.0
peak summed sample: 0.57 GiB at 2026-09-30T06:42:14Z (budget 10.00 GiB)
sum(mem_limit) for profiles core: 2.06 GiB
sum(mem_limit) with the 384 MiB W05 reserve: 2.44 GiB
VM MemTotal 11.66 GiB, minus 1 GiB leaves 10.66 GiB: limits ok
OOMKilled and RestartCount per container:
  bootstrap: OOMKilled=false RestartCount=0
  frankfurter: OOMKilled=false RestartCount=0
  frankfurter-init: OOMKilled=false RestartCount=0
  lakekeeper: OOMKilled=false RestartCount=0
  lakekeeper-migrate: OOMKilled=false RestartCount=0
  postgres: OOMKilled=false RestartCount=0
  seaweedfs: OOMKilled=false RestartCount=0
caveat: docker stats prints four significant digits and samples every 5 s, so a short peak can fall between samples; the OOM and restart checks cover it.
mem-report: ok
exit=0
```

Frankfurter's peak of 412.4 MiB sits just under its 416 MiB limit because docker stats counts the page cache, which the kernel reclaims at the limit. The cgroup's anonymous memory read 318 MiB (`memory.stat`, `anon`) at the end of the run, leaving about 98 MiB of headroom.

### Redaction

A planted capture built at runtime with `secrets.token_hex` values (an access key id, a secret access key, a DSN password, an STS session token and secret, and a path under the home directory) piped straight through the redactor. Only the redacted output is shown; the raw input was never printed or saved.

```text
$ python planted-capture | uv run python scripts/redact_evidence.py
s3.access-key-id=REDACTED
s3.secret-access-key=REDACTED
catalog dsn postgresql://lakekeeper:REDACTED@postgres:5432/lakekeeper
<SessionToken>REDACTED</SessionToken>
<SecretAccessKey>REDACTED</SecretAccessKey>
warehouse path <home>/Code/shopstream/infra/compose.yaml
```

### Canary hook

Recorded 2026-09-30, inside the disposable clone only, so the real repository's index was never touched. A Python one-liner read `CANARY_TOKEN` from the clone's `infra/.env` through `scripts/dotenv_lite.py` and wrote it into `canary-probe.txt` without printing it. The file was staged and `prek` ran the `canary-guard` hook over it. The hook failed and named the file, and the output holds the path but never the token. The probe file was then unstaged (`git rm --cached -q canary-probe.txt`) and deleted, `git status --porcelain` printed nothing, no commit was made in the clone, and `git log --all -- canary-probe.txt` prints nothing. The hook output was captured to a temporary file, passed through `scripts/redact_evidence.py`, and checked to hold no copy of the token.

```text
$ git add canary-probe.txt
$ uv run prek run canary-guard
canary token guard.......................................................Failed
- hook id: canary-guard
- exit code: 1

  canary-guard: CANARY_TOKEN from infra/.env found in canary-probe.txt
hook_exit=1
$ git rm --cached -q canary-probe.txt && rm canary-probe.txt && git status --porcelain
```

### Dependency groups

The default group installs nothing new, and the `spike` group adds the eight approved probe clients (with their dependencies, 44 packages in all). CI runs the default form.

```text
$ uv sync --locked --dry-run
Would use project environment at: .venv
Resolved 75 packages in 3ms
Checked 29 packages in 3ms
Would make no changes
$ uv sync --locked --group spike --dry-run
Would use project environment at: .venv
Resolved 75 packages in 3ms
Would install 44 packages
 + boto3==1.43.103
 + confluent-kafka==2.15.1
 + dlt==1.30.0
 + fastavro==1.12.2
 + pandas==3.0.6
 + polars==1.44.2
 + psycopg-binary==3.3.6
 + pyarrow==25.0.1
```

### Deviations

Phase 1 took these deviations from ADR-001, the requirements and the plans. ADR-001 itself is unchanged.

1. **`sts:AssumeRole` wording.** ADR-001 and PLAT-06 say the `lakekeeper` identity gets `sts:AssumeRole`. SeaweedFS 4.47 authorizes a named role through its trust policy alone, so the identity's actions are Read, List, Tagging and Write, the trust policy names only `arn:aws:iam::000000000000:user/lakekeeper`, and warehouse creation's vended-credential validation exercised that AssumeRole path.
2. **Bucket creation.** The `warehouse` bucket is created by the one-shot's SigV4-signed PUT (D-05). SeaweedFS runs without its `-bucket` flag, so that fallback was not taken.
3. **No SeaweedFS bisect, flag or file-mode change.** SeaweedFS stayed at the pinned 4.47 with no D-04 fallback, and both `iam.json` and the env file are mode 600 on virtiofs, so no mode ladder ran.
4. **Warehouse re-run status.** Lakekeeper 0.13.6 answers a second warehouse creation with `400 CreateWarehouseStorageProfileOverlap` instead of `409`; the one-shot maps that to `exists` only when the message names our own warehouse `spike`.
5. **Frankfurter memory limit.** The first provider backfill was OOM-killed at limits of 192m, 384m and 512m (192m was D-12's starting value), so the compose file was raised to 768m. `just mem-report` over a from-empty backfill at 768m (420 frames over 38 minutes) peaked at 310.4 MiB, and the plan's formula (that peak times 1.25, rounded up to a multiple of 32 MiB) gave 416m. The clean-clone run above, at 416m, ended with `OOMKilled=false` and `RestartCount=0` after the 35 minute backfill, but its docker-stats peak was 412.4 of 416 MiB, and one earlier run at 1g had peaked at 493 MiB of anonymous memory. The owner therefore set the final limit to 640m: 493 MiB times 1.25, rounded up to a multiple of 32 MiB, which covers the largest observed spike. A run that passed the OOM and restart check at 416m cannot start OOM-killing at a higher limit, so no new backfill measurement was taken at 640m; the memory tables in this page show the 416m clone run. The `core` mem_limit total, from `docker compose -f infra/compose.yaml config --format json`, is now 2.28 GiB (2336 MiB, including the two one-shots) and 2.66 GiB with the 384 MiB W05 reserve, under the 10 GiB test bound. Item 10 (FX offline) should re-check the limit.
6. **Where the clone lives.** Colima mounts only the home directory (and `/tmp/colima`) into the VM. A first clone under `/tmp` made `just up` fail: Postgres saw an empty init directory, skipped its scripts and Lakekeeper's migrate step failed with `password authentication failed for user "lakekeeper"`. The proof was rerun from a clone under the home directory.
7. **Postgres verification query.** The plan's replica-identity query needs `relreplident::text` on PostgreSQL 17; the query above includes the cast.

## Item 1: Credential scope

Not run.

## Item 2: Spark v3

Not run.

## Item 3: dbt v3

Not run.

## Item 4: Semantic layer

Not run.

## Item 5: CDC exactly-once

Not run.

## Item 6: Event-driven orchestration

Not run.

## Item 7: Gold blue/green

Not run.

## Item 8: RAM budget

Not run.

## Item 9: dbt login

Not run.

## Item 10: FX offline

Not run.

## Item 11: Two clocks

Not run.

## Item 12: Throughput

Not run.

## Item 13: Knob reachability

Not run.

## Item 14: Spark on Python 3.13

Not run.

## Item 15: PySpark version

Not run.
