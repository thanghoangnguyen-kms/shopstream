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

Recorded 2026-10-02.

Verdict: go. Figure 1 branch: "All engines: vended credentials", because vended credentials are scoped to their own table on Lakekeeper 0.13.6 and SeaweedFS 4.47, and only lakekeeper can assume the vended role.

### Versions

- Lakekeeper 0.13.6, `quay.io/lakekeeper/catalog:v0.13.6@sha256:d6829722cac0d00dfc5665b0955766387b93ccadd8b1e70e679b49619bc30ea5`.
- SeaweedFS 4.47, `docker.io/chrislusf/seaweedfs:4.47@sha256:ce9e796f1fe6f06968f4c04bdaf8f678dad9c8acdfef3d244133d71bfa6bf882`.
- Probe image base `python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`, with uv 0.12.18 (`sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`) installing the locked `spike` group.
- boto3 1.43.103, from `docker run --rm --entrypoint python shopstream-probe -c "import boto3; print(boto3.__version__)"`.

### Commands

The probe runs inside the Compose network, because Lakekeeper vends an S3 endpoint that the macOS host cannot resolve. It needs both profiles, because `probe` depends on `lakekeeper`. The script is bind-mounted, so the image holds no probe code and no key.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike build probe
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T probe
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T probe python /app/probe_scope.py
$ COMPOSE_PROFILES=spike uv run just up
```

The first run prints one JSON line and the second prints the matrix below. Both gave verdict go on one stack, one at a time. The last command exited 0 and created no probe service container, so the `spike` profile stays out of `just up`.

### Allow and deny matrix

Redacted output of the second run. Each denial is a 403 AccessDenied with a 2xx lakekeeper control on the same existing key.

```text
vended own put status=200 error=- expected=allow ok=True
vended own get status=200 error=- expected=allow ok=True
vended own list status=200 error=- expected=allow ok=True
vended own delete status=204 error=- expected=allow ok=True
vended own multipart status=200 error=- expected=allow ok=True
vended sibling get_metadata status=403 error=AccessDenied expected=deny ok=True
vended sibling get status=403 error=AccessDenied expected=deny ok=True
vended sibling put status=403 error=AccessDenied expected=deny ok=True
vended sibling list status=403 error=AccessDenied expected=deny ok=True
vended sibling multipart status=403 error=AccessDenied expected=deny ok=True
vended sibling delete status=403 error=AccessDenied expected=deny ok=True
vended lookalike put status=403 error=AccessDenied expected=deny ok=True
vended root get status=403 error=AccessDenied expected=deny ok=True
vended root put status=403 error=AccessDenied expected=deny ok=True
vended root list_prefix_empty status=403 error=AccessDenied expected=deny ok=True
vended root list_no_prefix status=403 error=AccessDenied expected=deny ok=True
vended root delete status=403 error=AccessDenied expected=deny ok=True
lakekeeper sibling get_metadata status=200 error=- expected=allow ok=True
lakekeeper sibling put status=200 error=- expected=allow ok=True
lakekeeper sibling get status=200 error=- expected=allow ok=True
lakekeeper sibling list status=200 error=- expected=allow ok=True
lakekeeper sibling multipart status=200 error=- expected=allow ok=True
lakekeeper sibling delete status=204 error=- expected=allow ok=True
lakekeeper root put status=200 error=- expected=allow ok=True
lakekeeper root get status=200 error=- expected=allow ok=True
lakekeeper root list_prefix_empty status=200 error=- expected=allow ok=True
lakekeeper root list_no_prefix status=200 error=- expected=allow ok=True
lakekeeper root delete status=204 error=- expected=allow ok=True
lakekeeper LakekeeperVendedRole assume_role status=200 error=- expected=allow ok=True
probe-other LakekeeperVendedRole assume_role status=403 error=AccessDenied expected=deny ok=True
admin LakekeeperVendedRole assume_role status=403 error=AccessDenied expected=deny ok=True
garbage LakekeeperVendedRole assume_role status=403 error=- expected=deny ok=True
observation vended own list_no_slash status=403 error=AccessDenied observed=deny
observation vended bucket list_buckets status=200 error=- observed=allow
vending with_header=True without_header=True without_header_prefix_matches_location=True without_header_config_vends=True expires_in_s=3600 prefix_matches_location=True
queues version=0.13.6 license_type=Apache-2.0 listed=['tabular_expiration', 'tabular_purge', 'task_log_cleanup'] openapi_queue_paths=['tabular_expiration', 'tabular_purge', 'task_log_cleanup'] expire_snapshots=absent orphan_removal=absent delete_profile=hard any_name_route_status=200
verdict: go (All engines: vended credentials)
```

The first run's verdict line, trimmed to its verdict and vending block:

```text
{"verdict":"go","branch":"All engines: vended credentials","vending":{"with_header":true,"without_header":true,"without_header_prefix_matches_location":true,"without_header_config_vends":true,"expires_in_s":3599,"prefix_matches_location":true}}
```

### Observations

- ListBuckets answers 200 with vended credentials. That exposes bucket names only; go criterion 1 concerns object access, which was denied.
- The list without the trailing slash is denied even inside the table, so only the trailing-slash list is scored.
- The credential expiry was 3599 s in the first run and 3600 s in the second, against the warehouse's 3600 s.
- The look-alike PUT to the key of table a followed by `x/probe.bin` is denied, so adjacent prefixes stay separate.

### loadTable without the delegation header

Without `X-Iceberg-Access-Delegation`, Lakekeeper 0.13.6 still returns `storage-credentials`. Its one entry's prefix is the table location (`without_header` true and `without_header_prefix_matches_location` true in the matrix block above). The top-level `config` also carries the three credential fields (`without_header_config_vends` true). No value was captured.

The probe records this as an observation, not a gate. ADR-001 go criterion 1 asks that `loadTable` with `vended-credentials` returns `storage-credentials`, and says nothing about the request without it.

An earlier run treated the no-header result as a gate and stopped as inconclusive. On 2026-10-02 the owner dropped that gate, and the probe was re-run with every other control unchanged.

Whether no-header vending matters for ADR-001 is the owner's question. This page does not change the ADR.

### Lakekeeper maintenance queues (PLAT-11)

`GET /management/v1/info`, reduced to its version, queues and license type:

```text
{"version": "0.13.6", "queues": ["tabular_expiration", "tabular_purge", "task_log_cleanup"], "license-type": "Apache-2.0"}
```

The expire-snapshots and orphan-removal queues are absent from Lakekeeper 0.13.6 (Apache-2.0; they are Lakekeeper+ features), so both are off: the queues are tabular_expiration, tabular_purge and task_log_cleanup, and the spike warehouse's delete profile is hard.

The per-name queue config route answered 200 for the made-up name `definitely_not_a_queue`, so that route proves nothing.

ADR-001's Decided-now row says 0.13.x "still has" both queues, while the measurement shows they are absent. The wording is the owner's to amend, and this page does not change it.

### FALL-01

FALL-01: N/A. Item 1's verdict is go, so every engine keeps vended credentials; no credential-mode switch, per-layer warehouse or full-hierarchy layout is needed.

Phase 6's settling PR mirrors this verdict into ADR-001's Results table.

## Item 2: Spark v3

Recorded 2026-10-02.

Verdict: fallback. Spark 4.1.3 wrote four format-version 3 tables with deletion vectors, and DuckDB 1.5.5 read all four at Spark's snapshot ids with Spark's row count and digest. PyIceberg 0.12.0 and Polars 1.44.2 cannot load a table with a VARIANT column, so per ADR-001 item 2 they read the payload as a JSON string column; both matched Spark on the two JSON-string tables, deletion vectors applied. No reader needs the v2 fallback.

### Versions

- Spark 4.1.3, Iceberg 1.11.0 and PySpark 4.1.3, with the driver and a Python UDF on Python 3.13.15 (the image from Item 14: base image digest and jar checksums are recorded there; image id `sha256:dc4a42f28a22492611bec28d32be471ad9023555535935463a7e1a4d25d91545`).
- Readers, all from the locked `spike` group in that image: DuckDB 1.5.5 with its iceberg extension 45163a28 (baked in `/opt/duckdb/extensions`, auto-install off), PyIceberg 0.12.0, Polars 1.44.2, pyarrow 25.0.1.
- Lakekeeper 0.13.6, SeaweedFS 4.47.

### Command

The writer and the three readers run one after the other in one `spark-job` container, so every reader sees the snapshots Spark just committed, and nothing else writes `spike_v3`. `read_v3.py` reads the job's JSON file, pins every read to the snapshot id Spark reported (DuckDB `AT (VERSION => id)`, PyIceberg `scan(snapshot_id=id)`, Polars `scan_iceberg(..., snapshot_id=id)`), and hashes each reader's Arrow rows through `scripts/row_hash.py`. The raw capture went to a temporary file outside both trees and through `scripts/redact_evidence.py`; the redactor changed nothing. Credentials are not printed: PyIceberg's vended storage keys and the Polars storage options live in memory only.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job sh -c 'python /app/spark_v3_job.py --out /tmp/spark.json && python /app/read_v3.py /tmp/spark.json'   (fields of the two JSON lines)
spark_version: 4.1.3
iceberg_version: 1.11.0
pyspark_version: 4.1.3
python_driver: 3.13.15
python_udf: 3.13.15
namespace: spike_v3
duckdb: 1.5.5 (iceberg extension 45163a28)
pyiceberg: 0.12.0
polars: 1.44.2
pyarrow: 25.0.1
verdict: fallback
fallback: pyiceberg -> JSON string column
fallback: polars -> JSON string column
reason: Spark wrote format-version 3 on all four tables, with added-dvs and PUFFIN delete files on b_json_dv and c_variant_dv
reason: duckdb matches Spark on all four tables
reason: pyiceberg cannot take the VARIANT tables (a_variant cannot-load, b_json_dv match, c_variant_dv cannot-load, d_json match), so it reads the payload as a JSON string column
reason: polars cannot take the VARIANT tables (a_variant cannot-load, b_json_dv match, c_variant_dv cannot-load, d_json match), so it reads the payload as a JSON string column
```

### Writer evidence

The four tables differ on purpose, so every reader failure is attributed to one feature:

- `a_variant`: VARIANT payload, one INSERT, no deletes. Fails only if a reader cannot take VARIANT.
- `b_json_dv`: JSON string payload, INSERT, then MERGE (update id 2, insert id 5), then DELETE of id 4, all merge-on-read. Fails only if a reader does not apply deletion vectors.
- `c_variant_dv`: the VARIANT payload with the same MERGE and DELETE. Matches only if a reader has both.
- `d_json`: JSON string payload, one INSERT, no deletes. The plain base read; a failure here is neither feature, so the verdict rule stops as inconclusive.

```text
a_variant: payload_type=variant format_version=3 snapshots=[append added-dvs=0] delete_files=[none]
b_json_dv: payload_type=json_string format_version=3 snapshots=[append added-dvs=0, overwrite added-dvs=1, delete added-dvs=1] delete_files=[content=1 PUFFIN records=1, content=1 PUFFIN records=1]
c_variant_dv: payload_type=variant format_version=3 snapshots=[append added-dvs=0, overwrite added-dvs=1, delete added-dvs=1] delete_files=[content=1 PUFFIN records=1, content=1 PUFFIN records=1]
d_json: payload_type=json_string format_version=3 snapshots=[append added-dvs=0] delete_files=[none]
all four table digests: 73e1c20d49efc16984df4fea2be2e99e7d3429d113fd5183e80eee0e447afa8b
```

The deleted row (id 4) is absent from every reader's rows: each loaded cell has four rows and Spark's digest, and an unloadable table is a `cannot-load` cell with empty counts, never zero rows. All four Spark digests are equal, so a VARIANT column read as JSON text and a JSON string column hash alike. A cell matches only when the row count and the table digest both equal Spark's. Readers may return rows in any order, because the table digest is order-free. Decimals compare as strings at the column's scale, timestamps as UTC microseconds and JSON numbers as normalised decimals, so the readers' different Arrow widths and time zones do not cause a mismatch on their own.

### Parity matrix

Spark is the reference. Each reader read the snapshot id in its row, and every one of the 8 cells a reader loaded reports that same id as its own current snapshot.

| Table | Reader | Result | Snapshot id | Rows | Digest (first 16 hex) |
| --- | --- | --- | --- | --- | --- |
| a_variant | spark | reference | 543517267814854887 | 4 | `73e1c20d49efc169` |
| a_variant | duckdb | match | 543517267814854887 | 4 | `73e1c20d49efc169` |
| a_variant | pyiceberg | cannot-load | 543517267814854887 | - | - |
| a_variant | polars | cannot-load | 543517267814854887 | - | - |
| b_json_dv | spark | reference | 3957586650624810269 | 4 | `73e1c20d49efc169` |
| b_json_dv | duckdb | match | 3957586650624810269 | 4 | `73e1c20d49efc169` |
| b_json_dv | pyiceberg | match | 3957586650624810269 | 4 | `73e1c20d49efc169` |
| b_json_dv | polars | match | 3957586650624810269 | 4 | `73e1c20d49efc169` |
| c_variant_dv | spark | reference | 1430565965759835334 | 4 | `73e1c20d49efc169` |
| c_variant_dv | duckdb | match | 1430565965759835334 | 4 | `73e1c20d49efc169` |
| c_variant_dv | pyiceberg | cannot-load | 1430565965759835334 | - | - |
| c_variant_dv | polars | cannot-load | 1430565965759835334 | - | - |
| d_json | spark | reference | 672250923678555355 | 4 | `73e1c20d49efc169` |
| d_json | duckdb | match | 672250923678555355 | 4 | `73e1c20d49efc169` |
| d_json | pyiceberg | match | 672250923678555355 | 4 | `73e1c20d49efc169` |
| d_json | polars | match | 672250923678555355 | 4 | `73e1c20d49efc169` |

Cannot-load error text, first line only by design: PyIceberg `ValidationError: 1 validation error for TableResponse` on `a_variant` and on `c_variant_dv`; Polars `PyIceberg load failed (ValidationError: 1 validation error for TableResponse)` on the same two, because Polars loads tables through PyIceberg. The first line does not name the field, so a one-off `load_table` of `spike_v3.a_variant` in the same container printed only the exception lines that contain `Unsupported field type`: `Value error, Unsupported field type: 'variant'`. That confirms PyIceberg 0.12.0 has no VARIANT type, and it fails the whole table, not just the column.

### Consequences

- PyIceberg and Polars take ADR-001 item 2's `JSON string column` fallback: a table that either must read keeps its payload as a JSON string column (`b_json_dv` and `d_json` show that path reads correctly, deletion vectors included). DuckDB and Spark need no fallback. No reader needs `v2 with position deletes`.
- The deletion-vector evidence on a VARIANT table (`c_variant_dv`) therefore rests on Spark and DuckDB alone; PyIceberg and Polars show deletion vectors only on the JSON-string table (G4: they only read v3).
- Deletion vectors mask rows; they do not erase them (G10). Erasure rewrites, expires snapshots and removes orphan files in a later week.
- This page does not change ADR-001 or the reference architecture. The reference-architecture change for the JSON-string fallback is the owner's `/shop-write-doc` hand-off in Phase 6's settling PR, and Phase 6 mirrors this verdict into ADR-001's Results table.

## Item 3: dbt v3

Recorded 2026-10-02.

Verdict: fallback. The owner chose ADR-001's fixed fallback (dbt-core 1.x with dbt-duckdb on standalone DuckDB 1.5.5) over dbt 2.0.6 for building Iceberg v3 tables, on the tested rule's `inconclusive` result: dbt 2.0.6 built the table at format-version 3 and merged the second batch, but its bundled DuckDB 1.5.4 writes deletion vectors that a second engine, DuckDB 1.5.5, rejects with "Deletion vector file is not a valid Puffin file (bad trailing magic)".

What this page does not show: the fallback runtime has not been run in this repo yet, so item 3's go criterion on the fallback is pending a Phase 3 replan, and stage 3's idempotency is unproven on a second reader. Research ran the same macro file on dbt-core 1.12.5 with dbt-duckdb 1.11.0 on 2026-10-02; that is a research observation, not evidence recorded here.

### Versions

- dbt 2.0.6 (`dbt --version` prints `dbt 2.0.6`). Licence: dbt Product Licensing Agreement (proprietary, free to use; the owner approved it on 2026-10-02 for this repo's local runs and CI, on the condition that the image stays local and is never pushed to a public registry). dbt-oss 2.0.5 is the Apache-2.0 alternative without `dbt lint`.
- DuckDB inside dbt 2.0.6, as dbt itself reports it through `select version()`: `v1.5.4`. ADR-001's Version matrix names 1.5.5 for the built-in DuckDB, so the two differ.
- DuckDB 1.5.5 for the reader in the spark-job image (the Item 2 reader).
- Lakekeeper 0.13.6, SeaweedFS 4.47.
- dbt image `shopstream-dbt-job`: id `sha256:522c0f3e01139aff3c54c70345bf881f5bfd5ebe81892ab6107f188dc3ec6c93`, 187330834 bytes. Base image `python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`, uv `0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`.

### Command

The project is the `analytics/dbt/lakekeeper` wrapper over the shared models: `catalogs.yml` with `use_catalogs_v2` and a `VENDED_CREDENTIALS` Lakekeeper catalog, and one incremental model, `silver_spike.inc_v3`, keyed on `id`, fed from a seed with a `batch` column. `iceberg.sql` supplies the `duckdb__create_table_as` macro (v3 comes from it, see Consequences) and the incremental strategy. One sequence ran in order: reset, batch 1, inspect, batch 2, inspect, batch 2 again, inspect. `inspect` reads `metadata.json` itself from SeaweedFS with the key `loadTable` vends, and keeps no credential in its output. The raw captures went through `scripts/redact_evidence.py`, which changed nothing.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/dbt_v3_check.py reset
{"reset": 204}
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job /opt/dbt/bin/dbt build --project-dir /work/analytics/dbt/lakekeeper --profiles-dir /work/analytics/dbt/lakekeeper --vars '{batch: 1}'
Finished 'build' successfully for target 'lk'
Processed: 1 model | 2 tests | 1 seed
Summary: 4 total | 4 success
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/dbt_v3_check.py inspect     (stage 1)
$ ... dbt build ... --vars '{batch: 2}'     (4 total | 4 success)
$ ... dbt_v3_check.py inspect     (stage 2)
$ ... dbt build ... --vars '{batch: 2}'     (4 total | 4 success, the rerun)
$ ... dbt_v3_check.py inspect     (stage 3)
$ uv run --frozen python scripts/dbt_v3_check.py verdict stage1.json stage2.json stage3.json
```

### Stages

All three stages share table uuid `01a0fc1a` and read `"format-version": 3` in `metadata.json`, agreeing with `loadTable`.

| Stage | Snapshot operations, in sequence order | Delete snapshot summary | DuckDB 1.5.5 rows |
| --- | --- | --- | --- |
| 1, batch 1 | append | none | (1, a), (2, b), (3, c) |
| 2, batch 2 | append, delete, overwrite | added-data-files 1, added-records 1, deleted-records 0 | error, see below |
| 3, batch 2 again | append, delete, overwrite, overwrite | same snapshots as stage 2, then one more overwrite (added-data-files 2, added-records 2) | error, see below |

Stage 1 passed: the one append is the first snapshot, and DuckDB 1.5.5 read the three expected rows. Stages 2 and 3 are the same table at later snapshots, and DuckDB 1.5.5 failed to read both with `Error: Deletion vector file is not a valid Puffin file (bad trailing magic)`. DuckDB's snapshot summaries carry no `added-dvs` key and no nonzero `deleted-records`, so the page cannot count the deletion vectors from them. The batch-2 snapshot history matches the expected two-commit shape (a delete, then an overwrite), and the stage 3 rerun still wrote one more overwrite.

The writer's own engine reads the right rows. A `dbt show --inline` read of the table after the rerun, on DuckDB v1.5.4, returned exactly `(1, a, false)` and `(2, b-updated, false)`, and dbt's `unique` and `not_null` tests passed in all three builds. That is dbt reading its own writes, not a second reader.

The rule's output, verbatim (exit 1):

```text
{"verdict": "inconclusive", "reasons": ["stage 2 carries an error: Error: Deletion vector file is not a valid Puffin file (bad trailing magic)", "stage 3 carries an error: Error: Deletion vector file is not a valid Puffin file (bad trailing magic)"]}
```

The rule returned `inconclusive`, not `fallback`: it judges a stage that errors as not decided. The `Verdict: fallback.` line above is the owner's decision on that result, recorded 2026-10-02, not the rule's output.

Cause, observed during the build step and not captured here: the deletion-vector files dbt wrote are 42 and 44 bytes, a raw blob with no Puffin header or footer. A control with standalone DuckDB 1.5.5 (one scratch v3 table, one DELETE, then purged) wrote a 336-byte file that starts and ends with `PFA1`, and 1.5.5 read it back.

### MERGE statements

dbt 2.0.6 writes no per-run compiled file for this strategy, so the two statements come from the run's query log, trimmed (the temporary table's name suffix is dbt's run id). The identical pair ran on the stage 3 rerun.

```sql
create temporary table "inc_v3__dbt_tmp_<run-id>" as (
    select id, name, is_deleted from "main"."changes" where batch = 2
);
merge into "lk"."silver_spike"."inc_v3" as d using "inc_v3__dbt_tmp_<run-id>" as s on (s.id = d.id)
    when matched and s.is_deleted then delete;
merge into "lk"."silver_spike"."inc_v3" as d
    using (select * from "inc_v3__dbt_tmp_<run-id>" where not is_deleted) as s
    on (s.id = d.id)
    when matched then update by name
    when not matched then insert by name;
```

### Consequences

- Silver and gold are not built by dbt 2.0.6. ADR-001's fixed fallback replaces it for building Iceberg v3 tables: dbt-core 1.x with dbt-duckdb on standalone DuckDB 1.5.5. The fallback has not been run in this phase, and its package pins are not yet approved. Item 3's go criterion on the fallback is pending the Phase 3 replan, so items 9 and 4 are replanned on it, not run on dbt 2.0.6.
- G11 (`sqlfluff-templater-dbt` needs dbt-core 1.x) is affected by the fallback: dbt-core 1.x is the runtime the fallback provides.
- dbt 2.0.6 ignores `iceberg_version` and `tblproperties` for DuckDB, so v3 comes from the project macro `duckdb__create_table_as`, which adds the `with ('format-version' = 3)` clause. The incremental strategy issues two MERGE INTO statements because DuckDB's Iceberg MERGE takes one UPDATE-or-DELETE action (dbt-labs/dbt#16018 tracks the DuckDB feature work).
- The macro file keeps dbt-duckdb 1.11.0's full macro signature, and research ran the same file on dbt-core 1.12.5 with dbt-duckdb 1.11.0 on 2026-10-02. That run is research and was not rerun here.
- The bundled DuckDB version (1.5.4, measured) and the Version matrix's 1.5.5 differ, an owner hand-off. Phase 6 mirrors this verdict into ADR-001's Results table.

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

Recorded 2026-10-02.

Verdict: go. An image whose Dockerfile FROM is pinned by digest (python:3.13.15-slim-trixie) plus Debian's OpenJDK 21 JRE and PySpark 4.1.3 from uv.lock ran item 2's job, and the driver and a Python UDF both reported Python 3.13.15.

### Versions

- Base image: `docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`; uv: `ghcr.io/astral-sh/uv:0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`. Both are copied from the Plan 02-03 Dockerfile, which the policy gate checks.
- JVM: runtime 21.0.12.1+1-1-deb13u1-Debian, vendor Debian (package `openjdk-21-jre-headless` from the Debian archive; apt is not hermetic, so the job prints the runtime version and a drift shows on a rerun).
- PySpark 4.1.3 installed by `uv sync --frozen --only-group spike --only-group spark`; Spark 4.1.3; Iceberg 1.11.0 (item 15 recorded why 4.1 and not 4.2).
- Jars, each added with `ADD --checksum=sha256:...` so a mismatch fails the build: `iceberg-spark-runtime-4.1_2.13-1.11.0.jar` `d6ea6c5d099288daeb7d5a92061bd3d7d8f296492632b42378e5f2f0e3066242`, `iceberg-aws-bundle-1.11.0.jar` `38f01da7e96850cdd05e6616d758b77b43314b712a8808e3f9a824d56976162f`. The build succeeded on the first try, so the plan's curl and `sha256sum -c` fallback was not needed.
- DuckDB 1.5.5 with the iceberg, httpfs and avro extensions baked into `/opt/duckdb/extensions` at build time, so no reader downloads one at run time.

### Image

The Dockerfile keeps the digest-pinned python:3.13.15-slim-trixie base. `docker image inspect` reports 948140818 B for the image; the VM disk readings below show what the build cost on disk. The jars and extensions are readable by uid 65534, the user the job runs as. The page's runtime section says the VM disk is 40 GiB, but `df` inside the VM shows a 59 GiB `/var/lib/docker` shared with unrelated images, so item 12 should use the `df` figure.

```text
$ grep -n ^FROM infra/spike/Dockerfile
5:FROM docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b
$ docker image inspect shopstream-spark-job --format {{.Id}} {{.Size}}
sha256:dc4a42f28a22492611bec28d32be471ad9023555535935463a7e1a4d25d91545 948140818
$ docker run --rm --user 65534:65534 --entrypoint sh shopstream-spark-job (sha256sum of the jars, baked extensions, Python)
38f01da7e96850cdd05e6616d758b77b43314b712a8808e3f9a824d56976162f  /opt/venv/lib/python3.13/site-packages/pyspark/jars/iceberg-aws-bundle-1.11.0.jar
d6ea6c5d099288daeb7d5a92061bd3d7d8f296492632b42378e5f2f0e3066242  /opt/venv/lib/python3.13/site-packages/pyspark/jars/iceberg-spark-runtime-4.1_2.13-1.11.0.jar
avro.duckdb_extension
httpfs.duckdb_extension
iceberg.duckdb_extension
Python 3.13.15
65534
$ colima ssh -- df -h /var/lib/docker   (before the build)
Filesystem      Size  Used Avail Use% Mounted on
/dev/vdb1        59G   30G   27G  54% /var/lib/docker
$ colima ssh -- df -h /var/lib/docker   (after the build)
Filesystem      Size  Used Avail Use% Mounted on
/dev/vdb1        59G   34G   22G  61% /var/lib/docker
```

### Command and output

The one-shot runs as uid 65534 on a read-only root filesystem with a 512 MiB `/tmp` tmpfs, a 3g memory limit and no key from `infra/.env` in its environment: Lakekeeper vends the storage credentials to the JVM. Both profiles are named because the job depends on Lakekeeper, which sits in `core`. The `/tmp` tmpfs is mounted `exec`: the Iceberg REST client decodes Lakekeeper's zstd-compressed responses with zstd-jni, which extracts a native library into `/tmp` and maps it, and a default noexec tmpfs refuses that. Tables are dropped through Lakekeeper's REST purge, because Spark's `DROP TABLE ... PURGE` also reads the dropped table's manifests on the client and lost a race with Lakekeeper's purge worker in one of five consecutive runs. Item 2's deletion-vector evidence (snapshot summaries and Puffin delete files) is in the Item 2 section; this section only needs the job to run.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job | tail -n 1   (fields of the JSON line)
spark_version: 4.1.3
iceberg_version: 1.11.0
pyspark_version: 4.1.3
java_version: 21.0.12.1+1-1-deb13u1-Debian
java_vendor: Debian
python_driver: 3.13.15
python_udf: 3.13.15
namespace: spike_v3
a_variant: format_version=3 snapshot_id=8839512430188009726 row_count=4 table_digest=73e1c20d49efc16984df4fea2be2e99e7d3429d113fd5183e80eee0e447afa8b
b_json_dv: format_version=3 snapshot_id=2055015646909726714 row_count=4 table_digest=73e1c20d49efc16984df4fea2be2e99e7d3429d113fd5183e80eee0e447afa8b
c_variant_dv: format_version=3 snapshot_id=4574780516556525914 row_count=4 table_digest=73e1c20d49efc16984df4fea2be2e99e7d3429d113fd5183e80eee0e447afa8b
d_json: format_version=3 snapshot_id=8304982815868503292 row_count=4 table_digest=73e1c20d49efc16984df4fea2be2e99e7d3429d113fd5183e80eee0e447afa8b
```

The four digests are equal: a VARIANT column read as JSON text and a JSON string column hash alike, and the two tables that went through a MERGE and a DELETE hash like the two that did not. Two consecutive runs, repeated three times, left the digests unchanged and changed the snapshot ids, so the job drops and recreates its tables.

### Memory

The job container, started with `docker compose run`, appears as `spark-job-run-<id>` in mem-report's per-service table with no `mem_limit` (the table only knows compose service names); its limit is the 3g pair in the compose file. It peaked at 845.1 MiB of 3 GiB, the peak summed sample over all sampled containers was 1.91 GiB, and mem-report exited 0. The job was not OOM-killed and exited 0, so the raise to 4g was not needed. Five frames, one every 5 s, covered the job; docker stats counts page cache and a short peak can fall between frames. This is input for item 8, which reruns this job in Phase 5. Mem-report's inspect scope (`core` plus `bootstrap`) does not include spike containers, so item 8's OOM and restart check needs them added.

```text
$ uv run just mem-report   (sampler: uv run just mem-sample --duration 300, stopped with SIGTERM when the job exited)
mem-report: 5 frames with container rows, 2026-10-02T03:08:53Z to 2026-10-02T03:09:13Z; 0 lines skipped, 0 frames without container rows
service                 peak MiB  mem_limit MiB
spark-job-run-caa6fe3afa63     845.1              -
seaweedfs                  515.6          768.0
frankfurter                383.4          640.0
postgres                   158.1          512.0
lakekeeper                  58.5          256.0
peak summed sample: 1.91 GiB at 2026-10-02T03:09:13Z (budget 10.00 GiB)
sum(mem_limit) for profiles core: 2.28 GiB
sum(mem_limit) with the 384 MiB W05 reserve: 2.66 GiB
VM MemTotal 11.66 GiB, minus 1 GiB leaves 10.66 GiB: limits ok
OOMKilled and RestartCount per container:
  bootstrap: OOMKilled=false RestartCount=0 Status=exited ExitCode=0
  frankfurter: OOMKilled=false RestartCount=0 Status=running ExitCode=0
  frankfurter-init: OOMKilled=false RestartCount=0 Status=exited ExitCode=0
  lakekeeper: OOMKilled=false RestartCount=0 Status=running ExitCode=0
  lakekeeper-migrate: OOMKilled=false RestartCount=0 Status=exited ExitCode=0
  postgres: OOMKilled=false RestartCount=0 Status=running ExitCode=0
  seaweedfs: OOMKilled=false RestartCount=0 Status=running ExitCode=0
mem-report: ok
```

### FALL-04

FALL-04: N/A. Item 14's verdict is go, so the official Spark image is not needed.

## Item 15: PySpark version

Recorded 2026-10-01.

Verdict: fallback. Maven Central has no Iceberg runtime for Spark 4.2 (iceberg-spark-runtime-4.2_2.13), in 1.11.0 or in 1.12.0, so Shopstream stays on PySpark 4.1 (4.1.3), which G4 names as a v3 writer.

### Versions

- Iceberg 1.11.0 is the pin in ADR-001's Version matrix. Iceberg 1.12.0 went GA on 2026-09-30 (GitHub release timestamp 2026-09-30T01:52:18Z), and its release script, `dev/stage-binaries.sh` at the `apache-iceberg-1.12.0` tag, stages Spark 3.5, 4.0 and 4.1 only. Neither release publishes a Spark 4.2 runtime, so the two releases are recorded separately here and neither stands in for the other.
- The 4.1 runtime artifact lists 1.11.0 and 1.12.0, in the order `maven-metadata.xml` gives them, so a rerun of the same lookup produces the same listing.
- PySpark 4.1.3 (2026-07-15) is the newest 4.1.x release. PySpark 4.2.0 exists (2026-07-14), but no Iceberg runtime does for it.
- Absence is shown by HTTP 404 on all four Spark 4.2 artifact paths plus `numFound` 0 from the search API, not by an empty listing alone.

### Commands and output

The lookups ran from the host, without a container, on 2026-10-01. The capture went to a temporary file outside both trees and through `scripts/redact_evidence.py`; it holds only public registry data, and the redactor changed nothing.

```text
$ curl -fsS https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.1_2.13/maven-metadata.xml | grep -E '<version>|<lastUpdated>'
<version>1.11.0</version>
<version>1.12.0</version>
<lastUpdated>20260930021942</lastUpdated>
$ curl -s -o /dev/null -w '%{http_code}' https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.2_2.13/maven-metadata.xml
404
$ curl -s -o /dev/null -w '%{http_code}' https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.2_2.13/1.11.0/iceberg-spark-runtime-4.2_2.13-1.11.0.jar
404
$ curl -s -o /dev/null -w '%{http_code}' https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-4.2_2.13/1.12.0/iceberg-spark-runtime-4.2_2.13-1.12.0.jar
404
$ curl -s -o /dev/null -w '%{http_code}' https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-4.2_2.13/maven-metadata.xml
404
$ curl -s 'https://search.maven.org/solrsearch/select?q=g:org.apache.iceberg+AND+a:iceberg-spark-runtime-4.2_2.13&rows=20&wt=json' | python3 -c 'import json,sys; print("numFound", json.load(sys.stdin)["response"]["numFound"])'
numFound 0
$ gh api repos/apache/iceberg/releases/tags/apache-iceberg-1.12.0 --jq .published_at
2026-09-30T01:52:18Z
$ gh api 'repos/apache/iceberg/contents/dev/stage-binaries.sh?ref=apache-iceberg-1.12.0' --jq .content | base64 -d | grep -m1 '^SPARK_VERSIONS='
SPARK_VERSIONS=3.5,4.0,4.1
$ curl -s https://pypi.org/pypi/pyspark/json (4.1.x and 4.2.x releases: version, upload date)
4.1.0 2025-12-16
4.1.0.dev1 2025-07-14
4.1.0.dev2 2025-09-28
4.1.0.dev3 2025-10-30
4.1.0.dev4 2025-11-20
4.1.1 2026-01-09
4.1.2 2026-05-21
4.1.3 2026-07-15
4.2.0 2026-07-14
4.2.0.dev1 2026-01-12
4.2.0.dev2 2026-02-08
4.2.0.dev3 2026-03-12
4.2.0.dev4 2026-04-10
4.2.0.dev5 2026-05-02
```

### Consequences

- ADR-001's go criterion 15 has two halves: Maven Central has a Spark 4.2 runtime, and item 2 passes on it. The first half fails, so the second has nothing to run, and item 2 runs on PySpark 4.1.3.
- The Version matrix pins stay as ADR-001 fixes them: Iceberg 1.11.0 and PySpark 4.1.x. A newer Iceberg release does not move a pin; moving to Iceberg 1.12 is an owner decision after the spike.
- AGENTS.md G4 is unchanged. PROV-02 updates it only if item 15 is go, and this item is fallback.
