# Week 2 spike evidence

Recorded from 2026-09-30.

Every capture passes through `scripts/redact_evidence.py` before it is written here, and is trimmed to about 40 lines. Each Item section gives the pinned versions, the exact command, the trimmed output, the date and a verdict line once its item runs. Evidence records counts and hashes, never generated PII values or the erasure canary.

## Platform base

Recorded 2026-09-30.

- Runtime: Colima 0.10.3, docker context `colima`, Docker Engine 29.5.2, Docker Compose 5.3.1.
- VM: Colima `default` profile on the Apple Virtualization framework (`vz`, virtiofs mounts), 12 GiB memory and 4 CPUs.
- Preflight: `docker info` MemTotal 12515225600 B against a threshold of 12348030976 B (D-03: the measured MemTotal rounded down to a multiple of 256 MiB).
- Stack: Compose project `shopstream`, profile `core` (postgres, lakekeeper-migrate, lakekeeper, seaweedfs) and the `bootstrap` one-shots. Frankfurter joins `core` later in this phase.

### First just up (skeleton)

Run from a state with no env file, no `infra/.generated/` and no named volumes. Output is redacted with `scripts/redact_evidence.py`; the Compose `Creating`, `Created`, `Starting`, `Waiting` and `Running` state lines are dropped, so the `Started`, `Healthy` and `Exited` lines remain. Exit code 0.

```text
uv run python scripts/stack.py up
VM memory ok: MemTotal 12515225600 B (11.66 GiB), threshold 12348030976 B (11.50 GiB)
env file: added 13 keys: POSTGRES_PASSWORD, LAKEKEEPER_DB_PASSWORD, AIRFLOW_DB_PASSWORD, SHOPSTREAM_DB_PASSWORD, LAKEKEEPER_PG_ENCRYPTION_KEY, SEAWEEDFS_ADMIN_KEY, SEAWEEDFS_ADMIN_SECRET, LAKEKEEPER_S3_KEY, LAKEKEEPER_S3_SECRET, PROBE_OTHER_KEY, PROBE_OTHER_SECRET, STS_SIGNING_KEY, CANARY_TOKEN
identity file: rendered
+ docker compose -f infra/compose.yaml --profile core up --wait --wait-timeout 300
 Container shopstream-postgres-1 Started 
 Container shopstream-seaweedfs-1 Started 
 Container shopstream-postgres-1 Healthy 
 Container shopstream-lakekeeper-migrate-1 Started 
 Container shopstream-lakekeeper-migrate-1 Exited 
 Container shopstream-postgres-1 Healthy 
 Container shopstream-lakekeeper-1 Started 
 Container shopstream-postgres-1 Healthy 
 Container shopstream-seaweedfs-1 Healthy 
 Container shopstream-lakekeeper-migrate-1 Exited 
 Container shopstream-lakekeeper-1 Healthy 
+ docker compose -f infra/compose.yaml --profile core --profile bootstrap run --rm warehouse
 Container shopstream-postgres-1 Healthy 
 Container shopstream-lakekeeper-migrate-1 Started 
 Container shopstream-lakekeeper-migrate-1 Exited 
 Container shopstream-postgres-1 Healthy 
 Container shopstream-lakekeeper-1 Healthy 
 Container shopstream-seaweedfs-1 Healthy 
 Container shopstream-bootstrap-1 Started 
 Container shopstream-seaweedfs-1 Healthy 
 Container shopstream-bootstrap-1 Exited 
 Container shopstream-lakekeeper-1 Healthy 
warehouse spike: created
+ docker compose -f infra/compose.yaml --profile core --profile bootstrap logs --no-log-prefix bootstrap
bucket warehouse: created
catalog bootstrap: created
```

Exit code: 0.

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
