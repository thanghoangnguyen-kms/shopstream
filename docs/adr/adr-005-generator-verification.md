---
title: "ADR-005: Generator verification"
type: adr
status: Proposed
owner: platform
decision: "Prove the generator's knobs, invariants and determinism with Hypothesis property tests from the workspace dev group, lint its clickstream contract with datacontract-cli from its own uv project under contracts/, and compare one SHA-256 per output stream across processes and against a committed golden manifest"
created: 2026-10-05
updated: 2026-10-05
depends-on:
  - adr-004-time-model.md
informs:
  - ../specs/transform/ref-bus-matrix.md
---

# ADR-005: Generator verification

## Context and Problem Statement

Week 3 builds the seeded generator in `packages/generator/`. It writes the messy Shopstream business into Postgres, emits clickstream events and reviews, and plants every messiness knob. Its proof is property tests in CI: FK integrity once late products land, monotonic `updated_at`, FX bounds, ECB currencies, each knob at its configured rate, the edit horizon, and the same seed giving the same output hash. The tools aren't in `uv.lock` yet, and `AGENTS.md` §1 requires an ADR before a new dependency. How does the generator prove its knobs, invariants and determinism in CI, with which tools, and against which default rates?

## Decision Drivers

- CI runs `uv sync --locked`, then `just test`; every Python tool is pinned in a lockfile ([ADR-000](adr-000-record-architecture-decisions.md)), and a local `just check` predicts CI.
- The same seed must give the same bytes in another process, time zone and `PYTHONHASHSEED`, and after a restart.
- A rate test must never flake, and must still fail a wrong rate.
- datacontract-cli 1.2.1 caps `pydantic<2.14` and, with its DuckDB extra, `duckdb<1.6` (checked 2026-09-25). In `uv.lock` those caps would bind the whole workspace.
- Tests run offline: no pytest test calls Frankfurter, Postgres or Kafka.
- Public CI logs and committed files are copies erasure can't reach, so no generated personal value or real canary token may land in them (ADR-001, Evidence rules).
- [ADR-004](adr-004-time-model.md)'s clock rules C1 to C10 each need a test.

## Considered Options

- Hypothesis in the dev group, datacontract-cli in its own uv project, per-stream hashes against a golden manifest
- Hypothesis and datacontract-cli both in the workspace dev group
- datacontract-cli through `uvx --from datacontract-cli==1.2.1`
- Example-based pytest only, with a same-process final-state hash

## Decision Outcome

Chosen option: "Hypothesis in the dev group, datacontract-cli in its own uv project, per-stream hashes against a golden manifest", because every tool stays pinned by a lockfile while the CLI's caps stay out of the workspace, and the determinism check covers what a same-process check misses: hash salting, time zones, restarts and the order of the output.

### Tools

| Tool | Pin | Installed in | Used for |
| ---- | --- | ------------ | -------- |
| Hypothesis | 6.168.1 | The workspace `dev` group in `uv.lock` (`uv add --dev hypothesis==6.168.1`) | Property and stateful tests in `packages/generator/tests/` |
| datacontract-cli | 1.2.1 | Its own uv project at `contracts/`, with its own `uv.lock` and the workspace's `exclude-newer = "3 days"` cooldown, outside the workspace like `analytics/dbt` | `just contract-lint`: `uv sync --locked --project contracts`, then `uv run --frozen --project contracts datacontract lint` on every `*.odcs.yaml` against ODCS v3.2.0 |
| pyyaml | 6.0.3, already in `dev` | — | The property tests read the contract's required fields and enums, so pytest never imports datacontract-cli |
| Python `random` | stdlib, Python `==3.13.*` | — | One `random.Random(f"{seed}:{knob}")` per knob. No NumPy, and no Faker: names and texts come from word lists in the package |

`just contract-lint` joins the `check` recipe's list and CI's `test` job, after `just test`; the four required checks stay as they are. Whether `datacontract lint` fetches the ODCS schema over the network is UNVERIFIED and gets checked when `contracts/` lands; CI's job has network either way, and pytest stays offline. `governance` owns the contract's content (`contracts/clickstream/page_view.odcs.yaml`), including PII classification of `customer_id`, `visitor_id` and the free-text `referrer` field, and `platform`'s generator conforms to it.

### Randomness, time and secrets

- Only `shopstream_generator/rng.py` constructs an RNG, with one sanctioned `# noqa: S311`. Adding a knob adds a stream and leaves the other knobs' output unchanged.
- Only integer draws (`randrange`, `getrandbits`, `choice`, integer-weighted `choices`) and comparisons of `random()` against a rate reach an output. The `libm`-backed variates (`expovariate`, `gauss`, `lognormvariate`) and `math` functions don't, because macOS and Linux can differ in their last bit; a test scans `packages/generator/src` for those names.
- Identifiers such as `event_id` come from the knob's stream (`uuid.UUID(int=rng.getrandbits(128), version=4)`), never `uuid.uuid4()`. Ruff's `TID251` bans `datetime.datetime.now`, `datetime.datetime.utcnow`, `datetime.date.today`, `time.time`, `uuid.uuid4` and the module-level `random` functions inside `packages/generator` (ADR-004 C1).
- Money is `Decimal` at the column's scale; no float reaches an output row.
- The canary token is config. CI and the golden manifest use a fixed public test token; a stack run reads `CANARY_TOKEN` from the untracked `infra/.env`, and a test fails if the two are equal whenever `CANARY_TOKEN` is set. The existing canary pre-commit hook still blocks any staged file holding the real token.
- The seeded injection review's body lives base64-encoded in a package data file with a warning header, so agents reading the source see no live instruction. It's decoded only when emitted, and the test holds its SHA-256.
- Assertions about personal values and the canary compare counts, hashes or booleans, never the values, so a failing assertion prints none; the state machine's steps carry ids and flags, not rows. `.hypothesis/` goes into `.gitignore`.

### Determinism hash

| Aspect | Rule |
| ------ | ---- |
| Scope | Every stream the generator emits: one per Postgres table (the operations it commits) and one per clickstream object (`page_view`) |
| Range | A run over `[t0, t1)` holds every operation and event whose emission tick is before `t1`; pending work stays in the checkpoint (ADR-004 C9), never flushed |
| Record | One canonical line per operation or event: JSON with sorted keys, separators `,` and `:`, UTF-8, `\n`-terminated. A Postgres operation holds its commit sequence number, the op, the primary key and the row it writes |
| Values | Timestamps as UTC ISO 8601 with microseconds; `Decimal` as a string at the column's scale; null as JSON `null`; a naive datetime or a float raises |
| Order | Emission order, unsorted: commit order (ADR-004 C2) and the clickstream knobs' delays are part of what's proven |
| Digest | SHA-256 per stream; the manifest maps each stream to its record count and digest, and its own SHA-256 is the run's hash |
| Golden | `packages/generator/tests/golden/manifest.json`, for a fixed seed, config, test canary token and 7 simulated days, written by `just generator-golden` and committed. It changes only in a commit that says why (an intended generator change, or a Python or Hypothesis bump) |

The determinism test runs that range in two subprocesses, one with `PYTHONHASHSEED=0` and `TZ=UTC` and one with `PYTHONHASHSEED=12345` and `TZ=Asia/Ho_Chi_Minh`, through the in-memory sink. Both manifests must equal each other and the golden, and on a mismatch the test prints each stream's count and digest, never its records. Because the output is platform-independent by construction, a laptop and CI produce the same golden; a disagreement is a portability defect that gets fixed, never skipped.

The Postgres write path is proven once in evidence, not in CI: two backfills with the same seed into fresh databases, each hashed as its final table rows sorted by canonical line, give identical hashes in `docs/evidence/w3-generator.md`. The same evidence run shows the checkpoint commits in the tick's transaction and that a kill between a tick's clickstream flush and its commit re-sends only that tick's events. The generator's tests add at most 3 minutes to CI's `test` job, and the evidence records the measured time from `pytest --durations`.

### Properties

| # | Kind | Property | Proves |
| - | ---- | -------- | ------ |
| P1 | Invariant | Each CDC table's final state, after the in-memory sink applies its operations, has a unique, non-null primary key equal to its [bus matrix](../specs/transform/ref-bus-matrix.md) grain key; `event_id` is unique among clickstream emissions that aren't planted duplicates | The declared grains |
| P2 | Invariant | Every order line committed before `t1 − K` has its product; every order, payment and review references rows that exist at its commit; an order's `ordered_at` and an identified page view's `event_ts` fall within its customer's `[created_at, deleted_at)`; no order line names a product after its `deleted_at` | FK integrity |
| P3 | Invariant | Transaction ticks rise strictly in commit order, every row carries its transaction's tick as `updated_at`, a key changes at most once per transaction except in a delete pair, and no SQL or DDL statement the generator issues reads a database clock | ADR-004 C1 to C3 |
| P4 | Invariant | `ordered_at` lies in `[created_at − D, created_at]`; no `orders` or `order_items` operation touches an order more than L behind the clock; every lifecycle completes within L | ADR-004 C4, C5 |
| P5 | Invariant | Each business hard delete follows an `UPDATE` of `updated_at` in its transaction; a soft-deleted customer never changes again; a `line_number` is never reused; payments are insert-only | ADR-004 C6 |
| P6 | Invariant | `0 ≤ order_discount ≤` the sum of the order's positive line amounts, and 0 when that sum is 0; refunds never exceed the order's captures; a payment's currency is its order's | Gold's allocation and sign rules |
| P7 | Invariant | The vendored ECB snapshot starts at least 4 days before the backfill; every rate is above 0 and moves at most 10 % between consecutive publications; every TARGET business day has a rate for every currency quoted then; EUR is 1.0 on every publication date; no gap exceeds 4 days | FX bounds, ADR-004 FX rule 2 |
| P8 | Invariant | Every order and payment currency is one of the quotes in the vendored latest ECB response (29 plus EUR) | ECB currencies |
| P9 | Oracle | `valid(record) == not record.malformed`, where `valid` reads the required fields and enums from `page_view.odcs.yaml` | The contract and the malformed knob agree |
| P10 | Statistical | Each rate knob's count over N = 50,000 trials, for seeds 1, 2 and 42, lies within the 5σ binomial bound of its configured rate | Each knob fires at its configured rate |
| P11 | Invariant | Each count knob fires exactly its configured number of times; the canary's token appears in every location the generator writes it to; the injection review exists once in the final state, is never moderated, and belongs to neither the canary nor a hot customer | The count knobs |
| P12 | Metamorphic | Same seed, config and range: the manifest equals the golden across two subprocesses; a backfill and a live run over one range give equal manifests; each stream of a run over `[t0, t1)` is a line-by-line prefix of the same stream over `[t0, t2)`; a run killed at `t1` and resumed to `t2` equals an uninterrupted run to `t2` | Determinism, ADR-004 C1, C8, C9 |
| P13 | Stateful | A `RuleBasedStateMachine` drives the live simulator (tick, large tick, insert, update, delete, late product) and checks after every step that the clock never moves back, no late product waits longer than K, a landed late product satisfies P2, and no order past L changed | The live simulator, ADR-004 C5 |
| P14 | Invariant | A backfill refuses to start on non-empty captured tables, or, once configured for Week 8, without the replication slot; with a fake ledger entry, the subject's ids appear in no stream and no checkpoint, including after a resume | ADR-004 C10 |

The vendored ECB snapshot holds the W2 Frankfurter rates from 2024-12-01 to 2026-10-02 and the latest response's quotes, under the package's data folder, so no test touches the network. The 10 % bound in P7 is a default the vendoring PR measures against the snapshot and records in the evidence.

### Default knob rates

All delays and windows run on the simulated clock.

| Knob | Path | Default | Unit |
| ---- | ---- | ------- | ---- |
| Late order | CDC | 2 %, with `ordered_at` up to D = 24 h before its insert; the edit horizon is L = 72 h, which leaves every lifecycle 48 h | Orders |
| Late-arriving product | CDC | 1 %, with the product's insert within K = 10 min of its first order line | New products |
| `ALTER TABLE` schema drift | CDC | Once: `customers` gains `tier` at 2025-07-01T00:00Z | Count |
| Erasure canary | CDC and clickstream | 1 customer, whose token sits in its `email` and `full_name`, in the `referrer` of its page views, and in one of its reviews | Count |
| Seeded prompt-injection review | CDC | 1 review with the fixed body above | Count |
| Duplicate events | Clickstream | 2 %, re-emitted with the same `event_id` and `event_ts` (the contract caps duplicates at 5 %) | Events |
| Out-of-order events | Clickstream | 1 %, emitted up to 90 s after a later event | Events |
| Events beyond the watermark | Clickstream | 0.5 %, emitted 15 to 60 min late, beyond the 10 min lateness bound | Events |
| Malformed events | Clickstream | 0.5 %, missing a required field or holding a `page_type` outside the enum | Events |
| Hot key | Clickstream | One product and one customer each hold a 0.2 share | Product page views; identified events |

The hot share, the 10 min lateness bound and the 90 s and 15 min delays are the values ADR-001 item 13 proved reach bronze. Business volumes (customers, orders and sessions per simulated day) are generator config, not knobs, and have no rate property.

### Hypothesis profiles

`packages/generator/tests/conftest.py` registers three profiles and loads one explicitly: `ci` (the built-in `ci` as parent, `max_examples=200`) when `CI` is set, otherwise `dev` (`deadline=None`, derandomized like `ci`), so a local `just check` and CI run the same inputs. An `explore` profile (`max_examples=5000`, `deadline=None`) runs on request, and each failure it finds becomes an `@example`. Rate tests use fixed seeds through `pytest.mark.parametrize`, never `st.randoms()`, whose values Hypothesis chooses rather than draws.

### Consequences

- Good, because the CLI's `pydantic` and `duckdb` caps can't hold back the workspace, and both lockfiles pin every transitive version.
- Good, because a determinism failure names the stream that changed, and the subprocesses, the prefix check and the resume check catch set-order, time-zone and restart leaks a same-process check misses.
- Good, because fixed seeds make the rate tests deterministic: a change that re-rolls the draws breaks a correct knob's bound with odds of about 1 in 1.8 million per seed, while a 2 % knob running at 3 % always fails.
- Bad, because `contracts/` adds a second side project to keep current, like `analytics/dbt`.
- Bad, because every intended generator change also regenerates and commits the golden manifest in the same PR.
- Bad, because at N = 50,000 the 5σ bound only resolves a 2 % knob's error of about half a percentage point or more.
- Bad, because the FX bound in P7 is unmeasured until the snapshot is vendored.

### Confirmation

- CI's `test` job fails on any failing property, on a manifest that differs from the golden, and on `just contract-lint`.
- The workspace's `members = ["packages/*"]` leaves `contracts/` out, as it leaves `analytics/dbt`, and mypy's strict mode covers `packages/generator`.
- Review rejects a PR that adds a generator dependency to `uv.lock` without an ADR, constructs an RNG outside `rng.py`, asserts on a personal value, or updates the golden without saying why.
- `docs/evidence/w3-generator.md` records the CI run that passes, the two Postgres-backed hashes, the one-writer rate and the measured max FX move, as counts and hashes only.

## Pros and Cons of the Options

### Hypothesis in the dev group, datacontract-cli in its own uv project, per-stream hashes against a golden manifest

- Good, because every version is locked and the caps are isolated.
- Good, because a mismatch names its stream, and the golden turns "deterministic" into "unchanged since the last deliberate update".
- Bad, because two lockfiles and a golden file need upkeep.

### Hypothesis and datacontract-cli both in the workspace dev group

- Good, because there's one lockfile.
- Bad, because `pydantic<2.14` and `duckdb<1.6` would bind every workspace package, starting with the DuckDB upgrade.

### datacontract-cli through `uvx --from datacontract-cli==1.2.1`

- Good, because there's no project to keep.
- Bad, because only the top-level version is pinned; its transitive versions float between runs, so a passing lint can't be reproduced.

### Example-based pytest only, with a same-process final-state hash

- Good, because it needs no new dependency.
- Bad, because a fixed example covers one input, and nothing shrinks a failure to its smallest case.
- Bad, because a same-process hash can't see `PYTHONHASHSEED` or time-zone leaks, and a final-state hash can't see the order of the changes.

## More Information

- [ADR-004](adr-004-time-model.md) sets the clock rules C1 to C10 that P2 to P5 and P12 to P14 test; D, L and K come from the table above.
- ADR-001's Knob paths table names each knob's path to bronze; Week 8 (CDC) and Weeks 6 to 9 (clickstream) rerun its checks on the real generator.
- The Hypothesis `ci` profile behaviour and the datacontract-cli caps were checked on 2026-09-24 and 2026-09-25.
