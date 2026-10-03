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

What this section's dbt 2.0.6 record does not show is a second reader's read of stages 2 and 3, because DuckDB 1.5.5 rejected them. The fallback runtime was run afterwards, and the Fallback runtime subsection below records it.

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

- Silver and gold are not built by dbt 2.0.6. ADR-001's fixed fallback replaces it for building Iceberg v3 tables: dbt-core 1.x with dbt-duckdb on standalone DuckDB 1.5.5. The Fallback runtime subsection below records that runtime and the rule's result on it, so items 9 and 4 run on the fallback, not on dbt 2.0.6.
- G11 (`sqlfluff-templater-dbt` needs dbt-core 1.x) is affected by the fallback: dbt-core 1.x is the runtime the fallback provides.
- dbt 2.0.6 ignores `iceberg_version` and `tblproperties` for DuckDB, so v3 comes from the project macro `duckdb__create_table_as`, which adds the `with ('format-version' = 3)` clause. The incremental strategy issues two MERGE INTO statements because DuckDB's Iceberg MERGE takes one UPDATE-or-DELETE action (dbt-labs/dbt#16018 tracks the DuckDB feature work).
- The macro file keeps dbt-duckdb 1.11.0's full macro signature, and research ran the same file on dbt-core 1.12.5 with dbt-duckdb 1.11.0 on 2026-10-02. The Fallback runtime subsection below reruns it in this repo.
- The bundled DuckDB version (1.5.4, measured) and the Version matrix's 1.5.5 differ, an owner hand-off. Phase 6 mirrors this verdict into ADR-001's Results table.

### Fallback runtime

Recorded 2026-10-02. The tested rule returned go on ADR-001's fixed fallback, dbt-core 1.12.5 with dbt-duckdb 1.11.0 on DuckDB 1.5.5, quoted in the Rule paragraph below.

#### Versions and licences

- `dbt --version` prints `installed: 1.12.5` for the core and `duckdb: 1.11.0` for the plugin. dbt's own `select version()` on the `lk` target prints `v1.5.5`, so the DuckDB inside dbt-duckdb is the Version matrix's 1.5.5, where dbt 2.0.6's bundled one was 1.5.4.
- Licences: dbt-core and dbt-duckdb are Apache-2.0, DuckDB is MIT. dbt 2.0.6 left the build, so the owner's earlier condition on the dbt Product Licensing Agreement no longer applies.
- dbt image `shopstream-dbt-job`: id `sha256:83a2dab73350ec6e33c0cb7e40fd5c9a941761802e246bad1fd62e9c4ba094a4`, 174707961 bytes. Base image `python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`, uv `0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`.
- The spark-job image reads the finished table with Spark 4.1.3 and Iceberg 1.11.0, and with PyIceberg 0.12.0. Lakekeeper 0.13.6, SeaweedFS 4.47.
- One transitive package is an sdist that downloads a binary at build time. dbt-core 1.12.5 requires `dbt-core-experimental-parser` 2.0.5, a 4.8 KB sdist with no wheel on PyPI, whose build backend downloads a platform wheel from the github.com/dbt-labs/dbt releases and fails on a sha256 that differs from the one inside the sdist. The lock pins the sdist's hash, and the image build and an uncached CI sync need outbound github.com. The owner approved this before the lock was written.

#### Project

One dbt project at `analytics/dbt`, in place of dbt 2's wrapper directory and `catalogs.yml`. Its `profiles.yml` has two targets. `lk` is an in-memory DuckDB that attaches the spike warehouse as alias `lk` (type `iceberg`, the Lakekeeper catalog endpoint, no authorization, vended credentials), and `ci` is in-memory with no attach and no extension. The model config key is `iceberg_catalog`: dbt-core reads the key `catalog_name` as the name of a catalog integration and stops with "Catalog not found" before any SQL runs. v3 still comes from the project macro `duckdb__create_table_as`, because dbt-duckdb emits a plain `create table`. The one incremental model is the same `silver_spike.inc_v3`, keyed on `id`, fed from a seed with a `batch` column.

#### Command

One sequence ran in order, each build selecting `+inc_v3` so it never touches other models. `inspect` reads `metadata.json` itself from SeaweedFS with the key `loadTable` vends, and keeps no credential in its output. The captures went through `scripts/redact_evidence.py`.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/dbt_v3_check.py reset
{"reset": 204}
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job /opt/dbt/bin/dbt build --target lk --select +inc_v3 --project-dir /work/analytics/dbt --profiles-dir /work/analytics/dbt --vars '{batch: 1}'
1 seed loaded, 1 incremental model created, 2 data tests ok (4 of 4 steps)
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/dbt_v3_check.py inspect     (stage 1)
$ ... dbt build ... --vars '{batch: 2}'     (4 of 4 steps ok)
$ ... dbt_v3_check.py inspect     (stage 2)
$ ... dbt build ... --vars '{batch: 2}'     (4 of 4 steps ok, the rerun)
$ ... dbt_v3_check.py inspect     (stage 3)
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/dbt_v3_check.py engines
$ uv run --frozen python scripts/dbt_v3_check.py verdict stage1.json stage2.json stage3.json engines.json
```

#### Stages

All three stages share table uuid `01a0fd53` and read `"format-version": 3` in `metadata.json`, agreeing with `loadTable`. DuckDB 1.5.5 read every stage without an error.

| Stage | Snapshot operations, in sequence-number order | DuckDB 1.5.5 rows |
| --- | --- | --- |
| 1, batch 1 | append | (1, a), (2, b), (3, c) |
| 2, batch 2 | append, delete, overwrite | (1, a), (2, b-updated) |
| 3, batch 2 again | append, delete, overwrite, overwrite | (1, a), (2, b-updated) |

The stage 1 append is the first snapshot in all three stages and the table uuid never changes, so the run merged into the table and did not recreate it. In stage 2 the delete snapshot added 1 delete file and 1 position delete (total-records 3), and the overwrite added 1 data file, 1 record, 1 delete file and 2 position deletes (total-records 4). Stage 3's extra overwrite added 1 data file, 1 record, 1 delete file and 1 position delete (total-records 5). Key 2 is updated in place, key 3 is deleted, key 1 is untouched, and dbt's `unique` and `not_null` tests on `id` ran in every build and succeeded, so no key appears twice. Running the merge a second time leaves the same two rows.

#### Second engines

Spark 4.1.3 with Iceberg 1.11.0 and PyIceberg 0.12.0 each read exactly (1, a, false) and (2, b-updated, false) from the table at stage 3. PyIceberg listed two delete files, and the script read each one's bytes through the table's own file access.

| Delete file | Format | Content | Size | PFA1 at the start | PFA1 at the end |
| --- | --- | --- | --- | --- | --- |
| 1 | PUFFIN | 1 | 336 bytes | yes | yes |
| 2 | PUFFIN | 1 | 338 bytes | yes | yes |

Every deletion vector is a valid Puffin file. dbt 2.0.6 wrote 42- and 44-byte raw blobs with no Puffin header or footer, which is what DuckDB 1.5.5 rejected above.

#### MERGE statements

dbt-core writes the compiled statements for the batch-2 build to `target/run/shopstream/models/silver_spike/inc_v3.sql`. The temporary table's name suffix is dbt's run id.

```sql
merge into "lk"."silver_spike"."inc_v3" as d using "inc_v3__dbt_tmp<run-id>" as s on (s.id = d.id)
    when matched and s.is_deleted then delete;
merge into "lk"."silver_spike"."inc_v3" as d
    using (select * from "inc_v3__dbt_tmp<run-id>" where not is_deleted) as s
    on (s.id = d.id)
    when matched then update by name
    when not matched then insert by name
```

#### Rule

The rule's output with the engines line as its fourth input, verbatim (exit 0):

```text
{"verdict": "go", "reasons": ["all three stages hold: format-version 3 in metadata.json, the append then the delete and overwrite merge commits, exactly (1, a), (2, b-updated), unchanged by the rerun", "a second engine family agrees: Spark with Iceberg and PyIceberg read exactly (1, a), (2, b-updated), and every deletion vector is a Puffin file with PFA1 magic at both ends"]}
```

The three-stage rule alone also returned go on the same three stage lines. The page's `Verdict: fallback.` line stays: it records the owner's choice of ADR-001's fallback over dbt 2.0.6, and this subsection records that the fallback meets item 3's criteria.

## Item 4: Semantic layer

Recorded 2026-10-02.

Verdict: go. MetricFlow 0.213.0 (mf 0.15.0), in its own uv project and venv, served `total_revenue` from the semantic manifest dbt-core 1.12.5 wrote for the dbt project, and `mf query` returned the same rows as hand-written SQL over gold, for the metric grouped by day and for the total. Each venv's `dbt` CLI works and reports dbt-core 1.12.5.

### Versions

- dbt-core 1.12.5 and dbt-duckdb 1.11.0 in both venvs: `/opt/dbt` builds the project and `/opt/mf` holds MetricFlow. `dbt-metricflow` 0.15.0 and `metricflow` 0.213.0 are in the MetricFlow venv only. The check's own DuckDB is 1.5.5, the same version dbt-duckdb loads.
- The MetricFlow venv comes from its own uv project (`analytics/metricflow`, 68 packages, a 175 KB lock) outside the workspace, synced with `uv sync --frozen`. The root workspace and its lock hold no dbt or MetricFlow package, and neither venv is on `PATH`: every CLI is called by its absolute path.
- Lakekeeper 0.13.6, SeaweedFS 4.47.
- dbt image `shopstream-dbt-job`: id `sha256:82737342b10cd6c2c2e6ba77b0dd99c8eae850894da88831b5543ab5ad861ac9`, 283952438 bytes (two venvs, against 174707961 for the one-venv image). Base image `python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`, uv `0.12.18@sha256:3adc3706091ce7c2fe595e669628caedd6d951551b92b258b7e7dbe06d9440bc`.
- Licences: dbt-core, dbt-duckdb, dbt-metricflow and MetricFlow are Apache-2.0, DuckDB is MIT.

### Command

Gold is two `incremental` models, `gold.fct_orders` and `gold.metricflow_time_spine`, each Iceberg format-version 3 with a `publish_id` column, built from a four-row `orders` seed into an empty `gold` namespace. A second build appends rather than replaces, so the sequence purges first. The semantic model `orders` and the metric `total_revenue` sit in `gold.yml` in the latest metrics spec, and `mf validate-configs` passed all seven stages. The captures went through `scripts/redact_evidence.py`.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/gold_reset.py gold
{"purged": {"gold": ["fct_orders", "metricflow_time_spine"]}}
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job /opt/dbt/bin/dbt build --target lk --select +tag:gold --project-dir /work/analytics/dbt --profiles-dir /work/analytics/dbt --vars '{publish_id: A}'
1 seed loaded, 2 incremental models created (3 of 3 steps ok)
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T -e DBT_PROJECT_DIR=/work/analytics/dbt -e DBT_PROFILES_DIR=/work/analytics/dbt dbt-job /opt/mf/bin/python /app/semantic_check.py
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T -e DBT_PROJECT_DIR=/work/analytics/dbt -e DBT_PROFILES_DIR=/work/analytics/dbt dbt-job /opt/mf/bin/mf query --metrics total_revenue --group-by metric_time__day --order metric_time__day --explain
```

`semantic_check.py` ran once for the page. It runs `mf validate-configs` and two `mf query` calls that write `--csv` files (the metric grouped by `metric_time__day`, then the total), runs the same two queries as SQL over `lk.gold.fct_orders` in DuckDB 1.5.5, and compares them by day with values as decimals. The purge, the build and the check ran as three separate commands in that order, so the rows below come from one purged build.

### Rows

| Day | MetricFlow | Hand-written SQL |
| --- | --- | --- |
| 2026-09-01 | 100 | 100.00 |
| 2026-09-02 | 75 | 75.00 |
| 2026-09-03 | 75 | 75.00 |
| Total | 250 | 250.00 |

The grouped result is in date order here. `mf query --csv` is not ordered by itself, so the check keys both sides by day and ignores row order, and it compares values as decimals, where 250 equals 250.00 and 75 does not equal 75.01. A day with no orders, such as 2026-09-04, is in neither result, and an empty result on either side is a mismatch, never a pass. The check's output, verbatim (exit 0):

```text
{"versions": {"dbt_cli": "1.12.5", "mf_dbt_cli": "1.12.5", "mf": "0.15.0", "metricflow": "0.213.0", "dbt_metricflow": "0.15.0", "duckdb": "1.5.5"}, "dbt_parse": {"exit": 0}, "validate_configs": {"exit": 0, "successful_stages": 7}, "mf_rows": [["2026-09-01", "100.0"], ["2026-09-02", "75.0"], ["2026-09-03", "75.0"]], "sql_rows": [["2026-09-01", "100.00"], ["2026-09-02", "75.00"], ["2026-09-03", "75.00"]], "mf_total": [[null, "250.0"]], "sql_total": [[null, "250.00"]], "comparison": {"grouped": {"match": true, "empty": false, "missing_days": [], "extra_days": [], "differing": [], "duplicate_days": []}, "total": {"match": true, "empty": false, "missing_days": [], "extra_days": [], "differing": [], "duplicate_days": []}}, "errors": [], "verdict": "go", "reasons": ["validate-configs and both mf queries succeeded", "MetricFlow's grouped and total rows equal the hand-written SQL's", "both dbt CLIs report dbt-core 1.12.x"]}
```

### Generated SQL

MetricFlow's `--explain` output for the grouped query, trimmed to the SQL. It reads `"lk"."gold"."fct_orders"`, the relation the dbt project built.

```sql
SELECT
  DATE_TRUNC('day', order_date) AS metric_time__day
  , SUM(amount) AS total_revenue
FROM "lk"."gold"."fct_orders" orders_src_10000
GROUP BY
  DATE_TRUNC('day', order_date)
ORDER BY metric_time__day
```

### Two dbt CLIs

Both venvs keep a working `dbt` CLI, each called by its absolute path, trimmed to the version lines:

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job sh -c '/opt/dbt/bin/dbt --version && /opt/mf/bin/dbt --version && /opt/mf/bin/mf --version'
/opt/dbt/bin/dbt   Core installed: 1.12.5   Plugin duckdb: 1.11.0
/opt/mf/bin/dbt    Core installed: 1.12.5   Plugin duckdb: 1.11.0
mf, version 0.15.0
```

ADR-001's go criterion 4 names `dbt` 2.0 and MetricFlow's `dbt-core`. Item 3 fell back to dbt-core 1.12.5, so the image holds no dbt 2.0 and the dbt 2.0 half was not exercised. This page reads the clause as: each venv's `dbt` works and both report dbt-core 1.12.5 (the owner's reading, recorded 2026-10-02).

Research history, from 2026-10-02 and not rerun: installing dbt 2.0.6 and `dbt-metricflow[dbt-duckdb]==0.15.0` into one venv (`uv pip install`) broke `dbt --version` with `ImportError: cannot import name 'ArtifactMixin' from 'dbt.artifacts.schemas.base'` and no resolver error. That is why MetricFlow keeps its own uv project and lock.

### Consequences

- MetricFlow stays in its own uv project with its own lock, ADR-001 item 4's go choice. The root workspace gains nothing, and a later move to one shared venv would be an owner decision, not a silent merge.
- On dbt-core 1.12.5, `mf` shares the dbt project's `profiles.yml` and queries through its default target `lk`, so no separate MetricFlow profile exists. `mf` has no `--target` flag, and research found it follows the default target or `DBT_TARGET`.
- `mf` reads `target/semantic_manifest.json`, which dbt-core writes. With no such file `mf query` stops with "Unable to load the semantic manifest", and any other target's run overwrites it: a `--target ci` run from `just check` left `"memory"."gold"."fct_orders"` in the manifest, and an earlier run of the check, before it refreshed the manifest, returned `fallback` on that. The check now runs `dbt parse --target lk --no-partial-parse` first, which rewrites the manifest for `lk` without building anything.
- The semantic YAML uses the latest metrics spec (the semantic model nested under the model, the metric with `agg_time_dimension`). dbt-core 1.12.5 accepts it, and research found 1.11.15 builds an empty manifest from it. Validation is `mf validate-configs`.
- The time spine sits in the same catalog as the gold models.
- Gold is built `incremental` into an empty namespace and purged before every rebuild, because DuckDB's Iceberg cannot rename or replace a table inside the transaction that creates it, and `table` materialization does exactly that. The purge refuses every namespace outside gold, gold_candidate and gold_retired.
- Phase 6 mirrors this verdict into ADR-001's Results table. The criterion's dbt 2.0 wording and the Version matrix's MetricFlow row (0.213.0 on dbt-core 1.12.5) are owner hand-offs.

## Item 5: CDC exactly-once

Recorded 2026-10-03.

The item's verdict follows the exactly-once and write-audit-publish checks recorded below. This part records the streaming stack those checks run on and the connector-secrets proof.

### Streaming stack

Versions, each image ref re-resolved on 2026-10-03 with `docker buildx imagetools inspect`; no tag had moved since it was pinned:

- Kafka 4.3.1, three combined broker and controller nodes in KRaft mode, `docker.io/apache/kafka:4.3.1@sha256:77e3df9054047a88b520d0cc46e16696d3b22022e1d580aeccd2632df6532837`.
- Debezium 3.6.3.Final on Kafka Connect 4.3.0, `quay.io/debezium/connect:3.6.3.Final@sha256:665fef453613d9cfca4b1ec5dccfa3ba0e6bf4db8f39d7e853a848657e0ec7fa`.
- Karapace 6.2.3, `ghcr.io/aiven-open/karapace:6.2.3@sha256:6d5b1ff1b77c497108be8be8359c9072294847cba5aa0767fc168ec70632d78f`.
- The Iceberg Kafka Connect sink 1.11.0, built from Apache Iceberg commit `6976e020b894f6a6777704df2b8c4458cb291ae9` in the `docker.io/library/gradle:8.14.4-jdk21-noble@sha256:cc90b5198f747c454f4a3e17495b626fbdfa7ac77f1ad08502c355e6f81e6772` builder. The sink is not published as a runtime artifact. The zip's sha256, read from `/kafka/iceberg-sink-zip.sha256` in the image, is `c19b586b48779c96ec260e182a884f463c6dff3279e30073bac7af19242a07c1`. It is recorded, not pinned, because Gradle resolves its dependencies at build time. This build reused the layer cache of an earlier build of the same Dockerfile, so Gradle did not run again; the recorded zip is the one inside the image.
- The Confluent Avro converter 8.3.2, fetched with a checksum check, sha256 `f849f49ae500d1c02f94a50d1dd4b3e63d26433843007ac114a7f8e9fb3d6be8`.
- The unpack stage reuses `docker.io/library/python:3.13.15-slim-trixie@sha256:7c61056e61ac89e852de05f3dc6fa51a6dd2181797bceed46aa725dd7cb2cd3b`.

Before the first image build, the owner confirmed that the converter and the builder image fall under ADR-001's decision, so no separate ADR was written for them.

Bring-up, by name so the core Frankfurter is never started: `docker compose -f infra/compose.yaml --profile core --profile streaming up -d --wait connect`. The topic one-shot and the Postgres CDC one-shot both exited 0, and Karapace and Connect reported healthy. Both connectors are registered with `python /app/connect_admin.py register` inside the spike image; the plugin list shows `io.debezium.connector.postgresql.PostgresConnector` 3.6.3.Final and `org.apache.iceberg.connect.IcebergSinkConnector` 1.11.0.

`kafka-topics.sh --describe`, reduced to one line per topic (name, partitions, replication factor, min.insync.replicas, cleanup.policy):

```text
__consumer_offsets 50 3 2 compact
__debezium-heartbeat.shopstream 1 3 2 -
__transaction_state 50 3 2 compact
_schemas 1 3 2 compact
connect-configs 1 3 2 compact
connect-offsets 5 3 2 compact
connect-status 5 3 2 compact
control-iceberg 1 3 2 -
fx.refresh 1 3 2 -
shopstream.public.customers 3 3 2 delete
shopstream.public.order_items 3 3 2 delete
shopstream.public.orders 3 3 2 delete
shopstream.public.products 3 3 2 delete
shopstream.public.reviews 3 3 2 delete
```

The CDC topics use `cleanup.policy=delete` because compaction would make bronze a legitimate superset of the Kafka set; ADR-002 owns the real policy. Karapace creates its `_schemas` topic at replication factor 3 (its default of 1 fails writes under min.insync.replicas 2 while its health route still answers ready).

Both connectors run with `tasks.max` 1. An idle sink task, one that never received a record, never answers the coordinator's commit request, so a three-task sink stalls every commit round for its 30 s timeout. The sink commits every 15 s (`iceberg.control.commit.interval-ms` 15000). Consumer groups: `connect-bronze-sink` (the sink's tasks), `connect-bronze-sink-coord` (its coordinator) and a transient `cg-control-<uuid>` group per worker; the source keeps its offsets in the `connect-offsets` topic. The control topic is `control-iceberg`.

The tracer row: one insert into `customers` landed in `bronze.customers` on the `audit` branch as the raw Debezium envelope, `op` c, an integer `source.lsn` of 53888848, Kafka partition 1 and offset 0. The table has an `audit` ref and no `main` ref, as the sink's first commit creates only the branch.

No Kafka, Karapace or Connect port is published (`docker compose ps` shows each with its container ports unpublished); every probe runs inside the Compose network.

Heartbeat observation. The connector sets `heartbeat.interval.ms` 10000 and the `__debezium-heartbeat.shopstream` topic grew from 33 to 36 records in 30 s. Idle-slot check: `confirmed_flush_lsn` of the `shopstream_dbz` slot read `0/33648C0` with the current WAL position at `0/3388F48`; after five `pg_logical_emit_message` calls in the `postgres` database (which the shopstream slot never decodes) and 90 s of waiting it still read `0/33648C0` against `0/3389228`, and the slot retained 147 kB. So the heartbeat did not advance the flush position of an idle slot here. `max_slot_wal_keep_size` of 4GB on Postgres is the bound, and no heartbeat table was added.

Memory, provisional until the load run measures it, from `docker stats --no-stream` on the five long-running streaming containers (the two one-shots have exited): connect 846.4 MiB of 1.5 GiB, karapace 111.7 MiB of 512 MiB, kafka-1 447.5 MiB, kafka-2 458.7 MiB and kafka-3 447.5 MiB, each of 1 GiB.

### Connector secrets (PLAT-08)

The Connect worker loads Kafka's `EnvVarConfigProvider` through three settings, which the Debezium image's entrypoint writes into the worker properties file:

```text
config.providers=env
config.providers.env.class=org.apache.kafka.common.config.provider.EnvVarConfigProvider
config.providers.env.param.allowlist.pattern=CDC_DB_PASSWORD
```

`grep -c` of the last line over the properties file prints 1. The Debezium connector's database password is the placeholder `${env:CDC_DB_PASSWORD}`; the real value reaches only the connect and Postgres CDC one-shot containers, through `infra/.env`. The connector running shows the placeholder resolves. The secret's name avoids the worker prefix because the entrypoint writes every variable with that prefix into its properties file and echoes its value in the log.

Each capture below was piped from the host into `uv run python scripts/connect_admin.py placeholders --key CDC_DB_PASSWORD --source <label>`, which reads the capture as UTF-8 with replacement, lists the distinct placeholders and counts the real password's plaintext occurrences, both raw and in its JSON-escaped form. A control run with the real value in the input counted 1, raw and inside JSON. The helper refuses a secret under 8 characters instead of counting a common string.

```text
{"source": "connector-json", "placeholders": ["${env:CDC_DB_PASSWORD}"], "plaintext_secret_occurrences": 0}
{"source": "rest-config", "placeholders": ["${env:CDC_DB_PASSWORD}"], "plaintext_secret_occurrences": 0}
{"source": "rest-tasks", "placeholders": ["${env:CDC_DB_PASSWORD}"], "plaintext_secret_occurrences": 0}
{"source": "config-topic", "placeholders": ["${env:CDC_DB_PASSWORD}"], "plaintext_secret_occurrences": 0}
{"source": "worker-log", "placeholders": [], "plaintext_secret_occurrences": 0}
{"source": "worker-properties", "placeholders": [], "plaintext_secret_occurrences": 0}
```

The sources, in order: the connector config as submitted (`show-config source`); `GET /connectors/shopstream-cdc`; `GET /connectors/shopstream-cdc/tasks`; every record of the `connect-configs` topic (12 records, read with `kafka-console-consumer.sh --from-beginning`); the worker log (6,165 lines); and `/kafka/config/connect-distributed.properties`.

The redaction pass masks any password-named field whole, so this proof is given as placeholders and counts instead of the raw connector JSON.

### Exactly-once

Exactly-once: go. Three serialized SIGKILLs of the Connect worker, two of them landing after DATA_COMPLETE and before COMMIT_COMPLETE, left bronze's (topic, partition, offset) set equal to the 14,559 non-null records a `read_committed` consumer reads below the fixed end offsets, with no duplicate offset, and the 14,389 distinct (primary key, `source.lsn`) changes equal the 14,389 `test_decoding` changes.

| Measure | Value |
| ------- | ----- |
| `count(*)` of bronze rows over the five tables | 14,559 |
| distinct (table, primary key, `source.lsn`) | 14,389 |
| Re-sends (`count(*)` minus distinct) | 170 |
| `test_decoding` changes, counted by `pg_logical_slot_peek_changes` | 14,389 |
| `test_decoding` changes, read by `pg_logical_slot_get_changes` to the same LSN | 14,389 |
| Kafka non-null records | 14,559 |
| Kafka tombstones | 2,416 |
| Missing offsets (in Kafka, not in bronze) | 0 |
| Extra offsets (in bronze, not in Kafka) | 0 |
| Duplicate offsets | 0 |
| Bronze rows at or beyond the end offsets | 0 |
| Audit-branch snapshots read | 150 |

The 170 re-sends are changes Debezium produced again after a restart: it resumes from its last flushed source offset, so the same `source.lsn` reappears at a new offset. They are not a failure; the silver macro collapses them across batches. The end offsets are the `read_committed` end offsets of the 15 partitions (three per topic), taken after the workload stopped and the sink caught up, once two reads 10 seconds apart agreed. Each is exclusive: the Kafka set holds the records below it, and a bronze row at or beyond it would count as beyond the end, not as extra. The end LSN for the `test_decoding` count was `0/39CC168`. `test_decoding` counted INSERT, UPDATE and DELETE lines for the five captured tables only: 2,389 DELETE, 6,605 UPDATE and 5,395 INSERT lines per the by-table counts, `cnt_slot` was created after the reset and before the first write, and the final measurement dropped it (no row of `cnt_slot` remains in `pg_replication_slots`).

### Kills

The kills ran one at a time. Each waited for both connectors to report RUNNING before the next began, and the next kill fired only on a later commit's log line. The kill triggers on a Connect log line because the commit window is under a second. The control topic classifies each kill by which events exist for the killed commit; the worker and the coordinator die together, so every event that exists was written before the kill.

| Kill | Stage targeted | Delay | Control-topic class | Recovered | Seconds from kill to the next completed commit |
| ---- | -------------- | ----- | ------------------- | --------- | ---------------------------------------------- |
| 1 | initiated | 0 ms | workers_writing | yes | 61.6 |
| 2 | ready | 200 ms | commit_to_table | yes | 63.1 |
| 3 | completed | 0 ms | commit_to_table | yes | 69.5 |

The seconds column is the time from the kill to the first table commit the restarted coordinator completed, read from the Connect log; the open control-topic transaction of the killed commit holds the partition until its 60 s timeout, so recovery takes about a minute. The harness reported kill 2's recovery after 23.2 s because its log follower replayed the killed worker's own last lines from the restart's one-second window; that was a harness defect, fixed afterwards, and it did not affect the kill, the classification or the measurement. The first ready attempt (200 ms after the "ready, received responses for all 15 partitions" line) landed in the window, so no other delay was tried. Events of the killed commit, then of the next commit in topic order, as event type and count in order:

```text
kill 1, killed commit A:  START_COMMIT
        next commit B:    START_COMMIT, DATA_WRITTEN x5, DATA_COMPLETE, COMMIT_TO_TABLE x5, COMMIT_COMPLETE
kill 2, killed commit C:  START_COMMIT, DATA_WRITTEN x5, DATA_COMPLETE, COMMIT_TO_TABLE x4
        next commit D:    START_COMMIT, DATA_WRITTEN x5, DATA_COMPLETE, COMMIT_TO_TABLE x2
kill 3, killed commit D:  START_COMMIT, DATA_WRITTEN x5, DATA_COMPLETE, COMMIT_TO_TABLE x2
        next commit E:    START_COMMIT, DATA_WRITTEN x5, DATA_COMPLETE, COMMIT_TO_TABLE x5, COMMIT_COMPLETE
```

Commit D is both kill 2's next commit and kill 3's killed commit: kill 2's restart ran D, and kill 3 then hit it after two of its five table commits. Kills 2 and 3 therefore each landed after DATA_COMPLETE and before COMMIT_COMPLETE (two of five table commits written in kill 3, four of five in kill 2). A kill classified commit_complete would prove nothing about recovery, because the commit had already finished; none occurred here. All control events decoded; none was undecodable.

### Delete and tombstone

One deleted key, from `customers`: key 9 produced bronze rows `c` at partition 0 offset 0, `u` at offsets 3 and 12, and `d` at offset 13, and its tombstone sits at offset 14 of the same partition, where bronze holds no row. The sink skips a null value but still advances the offset. Across the run the distinct `op=d` changes equal the Postgres DELETE count (2,389 each), and no bronze row sits at a tombstone offset (0 of 2,416). Tombstones outnumber deletes by 27 because Debezium emits one per delete record it sends, including re-sent ones; that split was not counted separately.

### Versions and workload

Debezium 3.6.3.Final, the Iceberg sink 1.11.0, confluent-kafka 2.15.1, pyiceberg 0.12.0 and psycopg 3.3.6, as the measurement reported them.

Workload command line, seed 5, one session at 20 transactions per second for 600 seconds (elapsed 600.002 s):

```text
docker compose -f infra/compose.yaml --profile core --profile streaming --profile spike run --rm -T cdc-run python /app/cdc_workload.py --mode item5 --seconds 600 --rate 20 --seed 5
```

The workload's own counts, by table and verb (a delete is an update then a delete in one transaction, so a table's `test_decoding` UPDATE count exceeds the workload's update count by its deletes):

| Table | Inserts | Updates | Deletes | Key range |
| ----- | ------- | ------- | ------- | --------- |
| customers | 1,066 | 824 | 428 | 2 to 1067 |
| products | 1,114 | 862 | 507 | 2 to 1115 |
| orders | 1,077 | 858 | 509 | 2 to 1078 |
| order_items | 1,063 | 840 | 457 | 2 to 1064 |
| reviews | 1,075 | 832 | 488 | 2 to 1076 |

The simulated business clock ran from 2026-01-01T00:00:00+00:00 to 2026-01-01T00:00:12+00:00, and the WAL position went from `0/34CB768` to `0/39BBB98` across the workload.

Before the run, the scoped reset: `rm -sf` of `connect`, `karapace`, `kafka-init`, `cdc-init` and the three brokers; removal of the three Kafka volumes; `cdc_check.py reset` (drops `shopstream_dbz` and `cnt_slot`, truncates the five captured tables, drops item 11's `tier` column if present, purges every bronze table through Lakekeeper); `up -d --wait connect`; `cdc_check.py slot-create`; `connect_admin.py register`. Phases 1 to 3's tables, the warehouse and the core Frankfurter were not touched.

Kill command, one per stage, from the host: `uv run --frozen python scripts/connect_kill.py --stage initiated --delay-ms 0 --timeout 240`, then `--stage ready --delay-ms 200`, then `--stage completed --delay-ms 0`. Each record was classified with `cdc_check.py classify` on stdin. Final measurement, with the kill records on stdin: `docker compose -f infra/compose.yaml --profile core --profile streaming --profile spike run --rm -T cdc-run python /app/cdc_check.py item5 --kills-stdin --final --timeout 600`.

### FALL-02

FALL-02: N/A. Item 5's exactly-once result is go (no lost change, no offset gap), so the Iceberg sink stays the only bronze CDC writer and no Spark Structured Streaming writer is added.

## Item 6: Event-driven orchestration

Not run.

## Item 7: Gold blue/green

Recorded 2026-10-02.

Verdict: fallback. Gold switches by table rename, because DuckDB 1.5.5 cannot read an Iceberg view that Lakekeeper lists; across 20 switches in each direction a DuckDB poller re-resolving every 100 ms with the publish-id rule's single retry never accepted a missing object or a mixed result, and PublishInProgress was 0.

### Versions

- DuckDB 1.5.5 with its iceberg extension (build `45163a28`) for the poller and the end-state checks.
- dbt-core 1.12.5 with dbt-duckdb 1.11.0 for the two gold builds.
- Lakekeeper 0.13.6, SeaweedFS 4.47.
- spark-job image `shopstream-spark-job`: id `sha256:dc4a42f28a22492611bec28d32be471ad9023555535935463a7e1a4d25d91545`.

### Command

Three gold objects, `fct_orders`, `dim_customers` and `metricflow_time_spine`, are `incremental` models, each Iceberg format-version 3 with a `publish_id` column. They are built twice from empty namespaces: into `gold_candidate` with publish_id B, then into `gold` with publish_id A, so gold ends on A. A second `incremental` build over existing objects would append, so the sequence purges first. The probe checks all of this before it starts (gold holds A on every object, gold_candidate holds B, every object has the `publish_id` column, `gold_retired` is empty) and again at the end. The captures went through `scripts/redact_evidence.py`.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/gold_reset.py gold gold_candidate gold_retired
{"purged": {"gold": ["dim_customers", "fct_orders", "metricflow_time_spine"], "gold_candidate": ["dim_customers", "fct_orders", "metricflow_time_spine"], "gold_retired": []}}
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job /opt/dbt/bin/dbt build --target lk --select +tag:gold --project-dir /work/analytics/dbt --profiles-dir /work/analytics/dbt --vars '{publish_id: B, gold_schema: gold_candidate}'
2 seeds loaded, 3 incremental models created (5 of 5 steps ok)
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T dbt-job /opt/dbt/bin/dbt build --target lk --select +tag:gold --project-dir /work/analytics/dbt --profiles-dir /work/analytics/dbt --vars '{publish_id: A}'
2 seeds loaded, 3 incremental models created (5 of 5 steps ok)
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T spark-job python /app/gold_switch_probe.py
```

The probe ran once for this page, with its defaults: 40 natural switches, 10 control switches, a poll every 100 ms, a retry delay of 250 ms, a hold of 1000 ms after each switch, a gap of 60 ms between objects in the control run, `max_table_staleness` 0s and the three objects above. Each switch renames every object in the fixed order above through Lakekeeper's REST rename in three steps: `gold` to `gold_retired`, `gold_candidate` to `gold`, `gold_retired` to `gold_candidate`. DuckDB's own `ALTER TABLE ... RENAME TO` stays inside one namespace, so it cannot do this. The poller runs on its own DuckDB connection, attached with `max_table_staleness` 0s so every read re-resolves metadata, and reads every object's `publish_id` through the publish-id rule: a read that finds two ids or an absent object is retried once after 250 ms, and a second bad read is PublishInProgress. The poll times and switch times come from `time.monotonic`. A poll is scheduled at a fixed rate; a poll that retries takes 250 ms longer, and the next one then starts at once, which is why the longest poll interval is above 100 ms. The probe's output, verbatim (exit 0):

```text
{"versions": {"duckdb": "1.5.5", "iceberg_extension": "45163a28"}, "settings": {"period_ms": 100, "retry_ms": 250, "hold_ms": 1000, "control_gap_ms": 60, "max_table_staleness": "0s", "objects": ["fct_orders", "dim_customers", "metricflow_time_spine"]}, "runs": {"natural": {"label": "natural", "switches": 40, "retry_ms": 250, "transitions": 40, "timing": {"poll_interval_ms": {"median": 99.98921300211805, "max": 286.3401899958262}, "switch_ms": {"median": 25.13380500022322, "max": 34.362017999228556}}, "a_to_b": 20, "b_to_a": 20, "gap_ms": 0, "polls": 417, "raw_ok": 412, "raw_mixed": 2, "raw_missing": 3, "retried_ok": 5, "publish_in_progress": 0, "accepted_mixed": 0, "accepted_missing": 0, "poll_errors": 0, "first_poll_error": null, "accepted_ids_seen": ["A", "B"], "accepted_sequence": "AAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAABBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAA"}, "control": {"label": "control", "switches": 10, "retry_ms": 250, "transitions": 10, "timing": {"poll_interval_ms": {"median": 99.9274539972248, "max": 297.31835999700706}, "switch_ms": {"median": 155.66600750389625, "max": 164.9648070015246}}, "a_to_b": 5, "b_to_a": 5, "gap_ms": 60, "polls": 117, "raw_ok": 107, "raw_mixed": 7, "raw_missing": 3, "retried_ok": 10, "publish_in_progress": 0, "accepted_mixed": 0, "accepted_missing": 0, "poll_errors": 0, "first_poll_error": null, "accepted_ids_seen": ["A", "B"], "accepted_sequence": "AAAAAAAAAAABBBBBBBBBBAAAAAAAAAAABBBBBBBBBBBAAAAAAAAAABBBBBBBBBBBAAAAAAAAAAABBBBBBBBBBAAAAAAAAAAABBBBBBBBBBBAAAAAAAAAA"}}, "end_state_ok": true, "view_check": {"view_created": true, "view_listed": true, "duckdb_reads_view": false, "duckdb_error": "CatalogException: Catalog Error: Table with name v does not exist!", "create_status": 200, "list_status": 200}, "verdict": {"verdict": "fallback", "path": "table rename", "reasons": ["DuckDB cannot read the Iceberg view that Lakekeeper lists, so view repoint is unavailable", "the natural run made 40 switches, 20 A to B and 20 B to A, and accepted no mixed or missing read", "PublishInProgress was 0 with a retry delay of 250 ms above the longest natural switch (34.362017999228556 ms)", "the control run counted 7 raw mixed and 3 raw missing reads, so the poller sees the window"]}}
```

### View check

A view `v` was created through Lakekeeper's REST API (HTTP 200) and listed by the namespace's views call (HTTP 200). DuckDB 1.5.5 could not read it. Its error text:

```text
CatalogException: Catalog Error: Table with name v does not exist!
```

The view and its namespace were dropped afterwards. View repoint is therefore unavailable with DuckDB as the reader, and ADR-001's fallback, table rename, is the path built.

### Natural run

Gold switched 40 times with no gap between objects, 20 from A to B and 20 from B to A, with the poller running from one hold before the first switch to one hold after the last.

| Count | Value |
| --- | --- |
| switches | 40 |
| a_to_b | 20 |
| b_to_a | 20 |
| polls | 417 |
| raw_ok (first read ok) | 412 |
| raw_mixed (first read mixed) | 2 |
| raw_missing (first read missing) | 3 |
| retried_ok (accepted after the retry) | 5 |
| accepted_mixed | 0 |
| accepted_missing | 0 |
| PublishInProgress | 0 |
| poll errors | 0 |
| transitions between accepted ids | 40 |
| publish ids accepted | A and B |

Timing: poll interval median 100.0 ms, maximum 286.3 ms; switch duration (three objects, nine renames) median 25.1 ms, maximum 34.4 ms. The retry delay, 250 ms, is above the longest natural switch, 34.4 ms. The raw mixed and missing reads were five polls that landed inside a switch; every one resolved on the single retry, which is the rule working as ADR-001 describes. Forty transitions for forty switches shows the poller saw every state change, so it was not reading a stale copy.

### Control run

The positive control: the same poller and rule over 10 switches (5 each way) with a gap of 60 ms between objects, which widens the window in which gold is mixed. It shows the poller can see the window.

| Count | Value |
| --- | --- |
| switches | 10 |
| a_to_b | 5 |
| b_to_a | 5 |
| polls | 117 |
| raw_ok (first read ok) | 107 |
| raw_mixed (first read mixed) | 7 |
| raw_missing (first read missing) | 3 |
| retried_ok (accepted after the retry) | 10 |
| accepted_mixed | 0 |
| accepted_missing | 0 |
| PublishInProgress | 0 |
| poll errors | 0 |
| transitions between accepted ids | 10 |
| publish ids accepted | A and B |

Timing: poll interval median 99.9 ms, maximum 297.3 ms; switch duration median 155.7 ms, maximum 165.0 ms. Ten raw mixed or missing reads, all retried and accepted once the switch finished: the poller sees the window, so zero accepted bad reads in the natural run is a result and not blindness. A natural run can see no mixed read by luck, which is why the page does not rest on the natural counts alone.

### Consequences

- ADR-001 item 7's fallback holds: table rename with the publish-id rule, and readers retry once on not-found as well as on a mixed read. A switch is atomic per object only, so a reader can see gold half-switched or an object absent between two renames; the retry covers both.
- The switch goes through Lakekeeper's REST rename, because DuckDB's own rename stays inside one namespace.
- `gold_candidate` keeps the retired gold copy after a switch, as ADR-001's credential section expects.
- Every gold reader keeps `max_table_staleness` at 0. DuckDB 1.5.5 re-resolves metadata on every read by default, and research found that a positive staleness (60 s) returned the old copy.
- Gold objects are `incremental` models built into empty namespaces and purged before every rebuild, because DuckDB's Iceberg cannot rename or replace a table in the transaction that creates it, and the `table` materialization does exactly that.
- Week 21 reuses `scripts/gold_switch.py`'s rule in the semantic layer and the MCP server; its own retry test starts from the 250 ms retry delay measured here.
- The reference-architecture change for this fallback is the owner's `/shop-write-doc` hand-off in Phase 6's settling PR, and Phase 6 mirrors this verdict into ADR-001's Results table.

## Item 8: RAM budget

Not run.

## Item 9: dbt login

Recorded 2026-10-02.

Verdict: go. On the item 3 fallback runtime (dbt-core 1.12.5 with dbt-duckdb 1.11.0), dbt build and docs lite ran in CI's required test job with no dbt login and no CI secret, and the dbt project's SQL is linted by the existing sqlfluff hook in the required lint job, because dbt-core has no lint and no login command.

ADR-001's criterion 9 is written in dbt 2.0 terms, and the build runs on dbt-core 1.12.5 since item 3's `fallback`. So this section maps each term onto the runtime in use, records what ran in CI and with no network, and keeps what a dbt 2 login sends apart from what was measured.

### Versions

- dbt-core 1.12.5 and dbt-duckdb 1.11.0 (`dbt --version`), DuckDB 1.5.5 (pinned in the `analytics/dbt` lock), sqlfluff 4.3.0, uv 0.12.18.
- CI: GitHub's `ubuntu-24.04` x86_64 runner, CPython 3.13.15. Local: macOS 27.0.1 on arm64, CPython 3.13.14.
- No-network runs: the `shopstream-dbt-job` image (`sha256:83a2dab73350ec6e33c0cb7e40fd5c9a941761802e246bad1fd62e9c4ba094a4`, the Item 3 fallback image), dbt-core 1.12.5 in `/opt/dbt`.
- Licences: dbt-core and dbt-duckdb are Apache-2.0, DuckDB and sqlfluff are MIT. No dbt licence condition applies, since dbt 2.0.6 left the build.

### Mapping

| ADR-001 criterion 9 term | On dbt-core 1.12.5 | Evidence below |
| --- | --- | --- |
| `dbt build` | `dbt build --target ci`: the in-memory `ci` target, no attach and no extension, so it needs no catalog and no stack | 4 of 4 steps ok locally, in CI and with `--network none` |
| `dbt lint` | does not exist: `dbt lint` prints `Error: No such command 'lint'.` The lint leg is the repo's sqlfluff hook (jinja templater, duckdb dialect) in the required lint job | the No such command line, the lint job's hook line |
| docs lite | `dbt docs generate --target ci --static`: `static_index.html` is 2429579 bytes locally, `catalog.json` is valid JSON with a `nodes` key holding 0 nodes, and `manifest.json` lists `model.shopstream.inc_v3`. Every dbt invocation opens a fresh in-memory DuckDB, so the catalog step connects and finds no built table | the target facts below |
| no login | there is no login: `dbt login` prints `Error: No such command 'login'. Did you mean 'clone'?`, no `dbt_cloud.yml` or `catalogs.yml` is tracked, and no workflow references a secret | the No such command line, the CI shape below |

### CI shape

One added step in the existing `test` job runs `uv run just dbt-ci`, the same recipe `just check` runs after `just test`. The required checks stay `lint`, `test`, `secrets` and `pr-title`, and no job starts Docker or the Compose stack. The `ci` target has no attach, so CI needs only pypi.org, files.pythonhosted.org and github.com (the experimental-parser wheel, see Consequences). `DO_NOT_TRACK=1` prefixes every dbt command in the recipe. `tests/test_ci_workflow.py` pins all of this: the four job names, no `pull_request_target` trigger, no `secrets.` expression and no `DBT_CLOUD` variable, the test job's three `run` steps in order, no Docker or Compose step, the `--target ci` and `DO_NOT_TRACK=1` prefix on each dbt line, and no tracked `dbt_cloud.yml` or `catalogs.yml`.

### Command

```text
dbt-ci:
    uv sync --locked --project analytics/dbt
    DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt build --target ci --project-dir analytics/dbt --profiles-dir analytics/dbt
    DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt docs generate --target ci --static --project-dir analytics/dbt --profiles-dir analytics/dbt
```

The local run's output, trimmed (the 59-line package list and the harmless `VIRTUAL_ENV` warning that `uv run just` prints are cut):

```text
$ uv run just dbt-ci
Installed 59 packages in 218ms
16:04:45  Running with dbt=1.12.5
16:04:45  Registered adapter: duckdb=1.11.0
16:04:46  Found 1 model, 1 seed, 2 data tests, 503 macros
16:04:46  Concurrency: 1 threads (target='ci')
16:04:46  1 of 4 OK loaded seed file main.changes ........................................ [INSERT 5 in 0.02s]
16:04:46  2 of 4 OK created sql incremental model silver_spike.inc_v3 .................... [OK in 0.02s]
16:04:46  3 of 4 PASS not_null_inc_v3_id ................................................. [PASS in 0.01s]
16:04:46  4 of 4 PASS unique_inc_v3_id ................................................... [PASS in 0.01s]
16:04:46  Completed successfully
16:04:47  Running with dbt=1.12.5
16:04:47  Building catalog
16:04:47  Catalog written to <repo>/analytics/dbt/target/catalog.json
```

The state it left, read from `analytics/dbt`:

```text
catalog.json bytes 326 nodes 0 sources 0
static_index.html bytes 2429579
manifest model nodes ['model.shopstream.inc_v3']
user.yml exists False
telemetry send lines in logs/dbt.log: 0
```

The two missing commands, with `DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt lint` and the same with `login`:

```text
Error: No such command 'lint'. (Did you mean one of: 'init', 'list'?)
exit=2
Error: No such command 'login'. Did you mean 'clone'?
exit=2
```

`dbt --version` prints `installed: 1.12.5` and `duckdb: 1.11.0`. It also asks PyPI for the latest version, so it is a networked command and is not part of the recipe.

### CI run

- Run: https://github.com/thanghoangnguyen-kms/shopstream/actions/runs/37031844664, event `pull_request` on draft PR 8.
- Head SHA: `26479b6a6d4b4f3b4b5d0345c28aa20d2c51868d`.
- Conclusion: success for all four jobs. The `test` job passed, its `Run uv run just dbt-ci` step passed (the test job took 21 s), and the `lint` job's sqlfluff hook passed. The `test` job's step `Run uv run just test` also passed, with 1214 tests.
- The first push was the only one: no CI run for this item failed and no fix was needed. An earlier run on the previous remote head (`5af4da96e4d9`) had also passed, before the dbt step existed.

The `test` job's dbt step, trimmed to what dbt and uv printed (package list, download lines and timestamps cut):

```text
uv sync --locked --project analytics/dbt
Using CPython 3.13.15
Creating virtual environment at: analytics/dbt/.venv
Resolved 62 packages in 0.74ms
   Building dbt-core-experimental-parser==2.0.5
      Built dbt-core-experimental-parser==2.0.5
Installed 59 packages in 42ms
DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt build --target ci --project-dir analytics/dbt --profiles-dir analytics/dbt
16:08:25  Running with dbt=1.12.5
16:08:25  Registered adapter: duckdb=1.11.0
16:08:26  Found 1 model, 1 seed, 2 data tests, 503 macros
16:08:26  Concurrency: 1 threads (target='ci')
16:08:27  1 of 4 OK loaded seed file main.changes ........................................ [INSERT 5 in 0.05s]
16:08:27  2 of 4 OK created sql incremental model silver_spike.inc_v3 .................... [OK in 0.05s]
16:08:27  3 of 4 PASS not_null_inc_v3_id ................................................. [PASS in 0.03s]
16:08:27  4 of 4 PASS unique_inc_v3_id ................................................... [PASS in 0.01s]
16:08:27  Completed successfully
DO_NOT_TRACK=1 uv run --frozen --project analytics/dbt dbt docs generate --target ci --static --project-dir analytics/dbt --profiles-dir analytics/dbt
16:08:28  Running with dbt=1.12.5
16:08:29  Building catalog
16:08:29  Catalog written to /home/runner/work/shopstream/shopstream/analytics/dbt/target/catalog.json
```

The `lint` job's hook line: `sqlfluff lint............................................................Passed`.

### No network

Each run used `docker run --rm --network none --memory 1g --memory-swap 1g --user 65534:65534 -e HOME=/tmp -v "$PWD/analytics/dbt:/src:ro" --entrypoint sh shopstream-dbt-job -c '<script>'` from the repo root. The script first copies `dbt_project.yml`, `profiles.yml` and the models, macros and seeds directories from `/src` to `/tmp/p`, never the host's `.venv`, `target/` or `logs/`, so nothing was written into the repo (`git status` over `analytics` stayed empty).

Build and docs, with `DO_NOT_TRACK=1`, `--project-dir /tmp/p --profiles-dir /tmp/p` and `--target ci`:

```text
$ /opt/dbt/bin/dbt build ...
16:09:42  1 of 4 OK loaded seed file main.changes ........................................ [INSERT 5 in 0.02s]
16:09:42  2 of 4 OK created sql incremental model silver_spike.inc_v3 .................... [OK in 0.03s]
16:09:42  3 of 4 PASS not_null_inc_v3_id ................................................. [PASS in 0.01s]
16:09:42  4 of 4 PASS unique_inc_v3_id ................................................... [PASS in 0.01s]
build exit=0
$ /opt/dbt/bin/dbt docs generate --static ...
16:09:43  Building catalog
16:09:43  Catalog written to /tmp/p/target/catalog.json
docs exit=0
-rw-r--r-- 1 nobody nogroup 2429838 Oct  2 16:09 /tmp/p/target/static_index.html
```

Telemetry, two `dbt parse --target ci` runs on fresh copies, both with `--network none`:

| Run | Settings | Telemetry send lines in `logs/dbt.log` | `.user.yml` |
| --- | --- | --- | --- |
| opted out | `DO_NOT_TRACK=1` and the project's `send_anonymous_usage_stats: false`, as the recipe runs | 0 | absent |
| control | `DO_NOT_TRACK` unset and the copy's `flags:` block deleted (on the copy, never the repo file) | 6 | created |

The control logged these events, each a `Sending event` line, and then `An error was encountered while trying to flush usage events`, because the container has no network, so nothing left it:

```text
'action': 'invocation', 'label': 'start'
'action': 'project_id', 'label': <project id>
'action': 'adapter_info', 'label': <project id>
'action': 'partial_parser', 'label': <project id>
'action': 'load_project', 'label': <project id>
'action': 'invocation', 'label': 'end'
```

Research's earlier unopted `dbt parse --target ci` logged 5 events (research, not a measurement here); this control measured 6.

### What a login sends

Measured on dbt-core 1.12.5, with no login. dbt-core sends anonymous usage statistics over HTTPS to a Snowplow collector, `fishtownanalytics.sinter-collect.com`, unless it is switched off. The control run above shows the events it prepares (6 for a `parse`, listed above) and the `.user.yml` file that holds its anonymous id. `DO_NOT_TRACK=1` and the project flag `send_anonymous_usage_stats: false` each gave 0 events and no `.user.yml`, and the recipe sets both. The generated `index.html` and `static_index.html` each contain the collector's hostname once. They were not opened in a browser, and whether opening them sends events was not tested.

dbt 2.0 behaviour, not exercised. No dbt 2 login ran in CI or anywhere in this item, since no dbt 2 is in the build. Only these facts from the cited docs.getdbt.com pages are recorded, and nothing beyond them is claimed:

- https://docs.getdbt.com/docs/deploy/dbt-state-about, "How is data stored in dbt State?": "dbt State sends the following metadata to dbt Labs servers: Last-modified timestamps: Used to determine whether upstream data has changed since the last run. SQL statement hashes: SQL statements are processed to detect and classify changes, then hashed. Only the hash is persisted for future comparisons. No actual data from your warehouse is transmitted."
- https://docs.getdbt.com/reference/commands/login: `dbt login` "will open browser-based authentication where you can sign in to your existing dbt platform account or create a free one", is available in dbt v2.0 and later, "doesn't support non-interactive authentication", and for CI/CD jobs the page says to use a service token instead of `dbt login`.
- The dbt Core v1 usage-stats text (the rendered docs.getdbt.com usage-stats page now shows only dbt 2 text) is in https://github.com/dbt-labs/docs.getdbt.com, `website/docs/reference/global-configs/usage-stats.md`. It says dbt Core v1 can be switched off with `flags: send_anonymous_usage_stats: false`, `DO_NOT_TRACK=1` or `DBT_SEND_ANONYMOUS_USAGE_STATS=False`.

The brief's "a login sends project metadata to dbt Labs" maps onto the dbt State fact above (last-modified timestamps and SQL statement hashes). What else a free login sends, for example for strict static analysis or SQL comprehension, is not documented in the pages read, so this page does not say.

### Linters

The sqlfluff hook (jinja templater, duckdb dialect, `.sqlfluff` unchanged) runs over every tracked SQL file, which includes the two under `analytics/dbt` (`macros/iceberg.sql` and `models/silver_spike/inc_v3.sql`). Run over those tracked paths it reports no violation:

```text
$ uv run --frozen sqlfluff lint analytics/dbt/models analytics/dbt/macros analytics/dbt/seeds
All Finished!
exit=0
```

A bare `sqlfluff lint analytics/dbt` over a synced tree is not equivalent. It also walks the gitignored `analytics/dbt/.venv`, which holds dbt's own package macros, and fails there (`adapters.sql` is the first file, with LT02 and JJ01 findings). The hook never sees those files, because it lints tracked files only, so the lint job is unaffected.

Research ran `sqlfluff-templater-dbt` 4.3.0 beside `sqlfluff` 4.3.0, dbt-core 1.12.5 and dbt-duckdb 1.11.0 in a throwaway virtual environment, with `templater = dbt` and the `ci` target, and it reported `violations: 0 status: PASS`. That is research, not rerun here, and it is Week 10's input. The templater is a new dependency, so adopting it needs an ADR first.

### Consequences

- No dbt token becomes a GitHub Actions secret, and no workflow references one. ADR-001's item 9 go branch, "No login and no CI secret", holds on the runtime in use.
- Every uncached CI sync builds the `dbt-core-experimental-parser` sdist, which fetches a sha256-checked wheel from github.com. The run above did this in under a second and passed, so the dependency on github.com for the first uncached sync is measured on `ubuntu-24.04`, not only assumed.
- The comment in `.sqlfluff` that names dbt 2.0 and G11 is now stale, because the fallback runtime is dbt-core 1.x. That is an owner decision for Week 10.
- Phase 6 mirrors this verdict into ADR-001's Results table, where criterion 9's dbt 2 wording is read against the dbt-core mapping above.

## Item 10: FX offline

Recorded 2026-10-02.

Verdict: go. Frankfurter 2.5.1, seeded online once from an empty volume with ECB only and the backfill confirmed complete, served the dlt pipeline through the v2 API with providers=ECB while the loader's network had no route out, and the loaded rates skip every weekend and every TARGET closing day. The full load covered 1999-01-04 to 2026-10-02 (the ECB end date on the seed day): 270,096 rows, 7,106 distinct non-EUR dates, 0 weekend rows, and the 134 weekdays with no rows are exactly the 134 TARGET closing weekdays, with 0 mismatches either way.

### Versions

- Frankfurter image: `docker.io/lineofflight/frankfurter:2.5.1@sha256:1fe11227574203d47535e784caa97fbb76b0798ac46bd09de28b7261067d4b32` (image id `sha256:1fe11227574203d47535e784caa97fbb76b0798ac46bd09de28b7261067d4b32`). The seed, the offline server and the volume owner use the same string as the core service.
- The loader runs in the spike image: dlt 1.30.0, DuckDB 1.5.5, Python 3.13.15, all read from the loader's own output.

### Seed

The seed is the `frankfurter-seed` one-shot on the empty `frankfurter-fx-data` volume, the only service with a route to the ECB. It runs once, online, as uid 1000 under a 640m limit, after `frankfurter-fx-init` has chowned the volume, and it exits before `frankfurter-offline` starts.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T frankfurter-seed
  entrypoint: sh -c "bundle exec rake db:setup && bundle exec rake 'backfill[ECB]'"
I, [2026-10-02T16:48:29.433202 #9]  INFO -- : ECB: backfilling from 1999-01-04
I, [2026-10-02T16:48:32.439572 #9]  INFO -- : ECB: inserted 8840 rates
  ... 26 more chunks, one per year of 5,700 to 11,100 rates ...
I, [2026-10-02T16:50:56.637662 #9]  INFO -- : ECB: inserted 7620 rates
exit 0, wall time 2:32.35 (the first chunk logged 16:48:29, the last 16:51:00)
```

`db:setup` and `backfill[ECB]` both worked on the fresh volume with no extra task to register providers. Memory peak from `just mem-report` over the 33 frames sampled during the seed (one every 5 s, so a short peak can fall between samples): the seed container peaked at 126.7 MiB against its 640 MiB limit. The report's exit code was 1, because the core frankfurter container was OOM-killed in an earlier run and was not running to be sampled (see Observations); that was expected and was not changed.

```text
service                 peak MiB  mem_limit MiB
seaweedfs                  180.7          768.0
postgres                   131.1          512.0
frankfurter-seed-run       126.7              -
lakekeeper                  33.0          256.0
```

Completeness is read from `GET /v2/providers`, not from the root route (which is healthy long before a backfill ends). The ECB entry after the seed:

```text
name: European Central Bank
start_date: 1999-01-04
end_date: 2026-10-02
publishes_missed: 0
```

The loader refuses to load unless the start date is 1999-01-04, `publishes_missed` is 0 and the end date is at most 4 days before the run date (no ECB publication gap since 1999 is longer than 4 days).

### Network cut

`fx_offline` is a Compose network with `internal: true`. Only `fx-load` and the web-only `frankfurter-offline` (puma without the scheduler, network alias `frankfurter`, no published port) sit on it; no other service sets a network. The loader shows the cut from inside its own container before it loads, and reads the providers entry through the Frankfurter on the same network:

```text
dns_blocked: true   getaddrinfo for data-api.ecb.europa.eu: [Errno -3] Temporary failure in name resolution
ip_blocked: true    TCP connect to 1.1.1.1 port 443 (5 s timeout): [Errno 101] Network is unreachable
http://frankfurter:8080/v2/providers answered with the ECB entry above
```

### Load

The loader is `scripts/fx_load.py`, run as the `fx-load` one-shot. It fetches `GET /v2/rates?providers=ECB&from=<year start>&to=<year end>` once per calendar year, drops each range's carry-in anchor row (a range that starts on a non-publication day also returns the previous publication date), and loads into DuckDB with dlt (pipeline `fx`, dataset `bronze`, table `fx_rates`, merge on date, base and quote). dlt telemetry is off. No random seed exists for this script; the seed command above is its other input.

```text
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T fx-load python /app/fx_load.py --from 2025-01-01
from 2025-01-01 to 2026-10-02: rows 13695, distinct non-EUR dates 448, min date 2025-01-02, max date 2026-10-02, weekend rows 0, load 0.83 s
$ docker compose -f infra/compose.yaml --profile core --profile spike run --rm -T fx-load python /app/fx_load.py
from 1999-01-04 to 2026-10-02: rows 270096, distinct non-EUR dates 7106, min date 1999-01-04, max date 2026-10-02, weekend rows 0, load 12.66 s
```

The first run's minimum date is 2025-01-02, so the anchor row dated 2024-12-31 did not survive. `--api v1` was not needed, so the v1 path (`/v1/<from>..<to>`, ECB only) was not run.

### Gaps

Over the full range the loader compares the distinct non-EUR dates (the EUR/EUR identity record is left out) with the weekdays of the range and the TARGET calendar:

```text
weekdays in range: 7240
missing weekdays: 134
TARGET closing weekdays in range: 134
missing weekdays that are not closing days: []
closing days that hold rows: []
```

The calendar rule is 1 January, 25 and 26 December every year; Good Friday, Easter Monday (from the date of Easter via `dateutil.easter`, already in the lock through pandas) and 1 May from 2000; 31 December through 2001. Easter was not a closing day in 1999, so 1999-04-02 and 1999-04-05 hold rows. This rule is inferred from the data and general TARGET knowledge, not read from an ECB page, which is why the check prints every mismatch both ways instead of assuming the rule. No holiday package was added.

### Observations

- The core `frankfurter` container's current state: `exited, OOMKilled=true, RestartCount=0`. It was left untouched.
- An earlier look on 2026-10-02 found the core service, which runs every provider's scheduler, OOM-killed twice on its filled volume and never finishing the ECB backfill (about 1.3 GB of SQLite plus a 133 MB WAL). The cause is assumed, not isolated. The seed here fetched ECB only and finished in 2.5 minutes under a peak of 126.7 MiB.
- Web-only Frankfurter (`frankfurter-offline`): 88.5 MiB peak against its 256 MiB limit, from `just mem-report` over 7 frames (30 s) during a full-range load. The loader container peaked at 887.3 MiB against its 1g limit in the same report; docker stats counts the DuckDB file on the `/tmp` tmpfs and the page cache, so the loader has little headroom if the range or the table grows.
- `frankfurter-offline` was removed (`rm -sf`) before the seed and after every load, so the SQLite file never had two writers, and the core frankfurter never mounts `frankfurter-fx-data`.

### Consequences

- ADR-001 item 10: go. v2 with `providers=ECB` works; v1 was not needed, so no fallback line applies.
- The knob "FX weekend and holiday gaps" reaches `bronze.fx_rates` through dlt, here in the loader's own DuckDB file. How dlt writes to Lakekeeper is a Week 9 decision.
- Phase 6 mirrors this verdict into ADR-001's Results table.

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
