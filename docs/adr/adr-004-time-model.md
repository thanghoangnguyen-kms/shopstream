---
title: "ADR-004: Time model"
type: adr
status: Proposed
owner: analytics-eng
decision: "Per key, `source.lsn` orders CDC changes; the simulated `updated_at`, which one checkpointed generator writer assigns in commit order, drives SCD2 validity as half-open UTC intervals; event times on the same simulated clock date every fact and clickstream event; `source.ts_ms` only measures freshness"
created: 2026-10-05
updated: 2026-10-05
depends-on:
  - adr-001-feasibility-spike.md
informs:
  - ../specs/transform/ref-bus-matrix.md
  - ../specs/platform/ref-architecture.md
  - adr-005-generator-verification.md
---

# ADR-004: Time model

## Context and Problem Statement

Every Shopstream change carries several timestamps: its WAL position (`source.lsn`), the commit's wall time (`source.ts_ms`), Debezium's and the sink's processing times, and the business times the generator writes on its simulated clock. The simulated history spans months, while the commits that carry it span minutes of real time. ADR-001 item 11 measured the two candidates: per key, `source.lsn` matched commit order for 21,658 changes over 8,958 keys (0 violations), but three concurrent writers produced 2 `updated_at` inversions in 9,905 comparisons where one writer produced 0 in 671. Which clock orders CDC changes, which drives SCD2 validity and dates the facts, and what must the generator guarantee so the two agree?

## Decision Drivers

- Silver and every SCD2 dimension are pure functions of bronze: the same change log gives the same tables in any arrival order, so a rebuild matches gold by row hash (INV-04, INV-09).
- Validity and fact dates must reflect business time. A version that lasts four simulated months can't be dated by the minutes its commits took.
- ADR-001 item 11: an `updated_at` tie or inversion is a generator defect, never a case the dbt macro works around.
- The generator is deterministic: the same seed, config and erasure ledger give the same output ([ADR-005](adr-005-generator-verification.md)).
- Reconciliation (INV-10) needs a cutoff after which the source no longer changes.
- An erased subject must never come back, including through a generator that runs again (INV-05).

## Considered Options

- LSN orders, simulated `updated_at` drives validity (two clocks)
- Commit time (`source.ts_ms`) orders and drives validity
- Simulated `updated_at` orders and drives validity
- `source.sequence` orders, simulated `updated_at` drives validity

## Decision Outcome

Chosen option: "LSN orders, simulated `updated_at` drives validity", because each clock is then right for its one job. The LSN is commit order for one key, as item 11 measured, and it needs nothing from the generator. The simulated `updated_at` is the business time a version starts, and making one writer assign it in commit order removes the inversions item 11 found. `source.sequence` stays the documented fallback for a global order, which no model needs today.

### Clocks

| Clock | Comes from | Its one job | Never used for |
| ----- | ---------- | ----------- | -------------- |
| `source.lsn` | The change's WAL position | Orders one key's changes in silver and SCD2 | Validity, fact dates |
| Simulated `updated_at` | The generator's clock, assigned in commit order | SCD2 validity `[valid_from, valid_to)` | Ordering |
| Event time: `ordered_at`, `paid_at`, a review's `created_at`, `event_ts` | The generator's clock | Fact date roles and as-of lookups, the FX lookup, the clickstream lateness rule and session windows | Ordering CDC changes, validity |
| `source.ts_ms` | The commit's wall time | The freshness SLI: age of the newest gold row against the source commit | Any model column |
| Envelope `ts_ms`, the sink's ingestion time, the Kafka record timestamp | Wall clocks | Lag between two hops | Any model column |

### Ordering rules

1. Silver and SCD2 sort one key's changes by `source.lsn` and deduplicate on (key, `source.lsn`). Kafka offset, arrival order and ingestion time never order changes.
2. The initial snapshot's rows (`op=r`) come once per key, before that key's streamed changes (item 11), so the history backfill runs only with Debezium streaming (Week 8) or on a fresh database after Week 8 (C10). A later re-snapshot row is ordered by its LSN like any change.
3. No model needs a global order across keys. One that does uses `source.sequence` parsed as two integers and compared as a pair, never as text and never `source.lsn` alone.

### Validity rules

1. A version's `valid_from` is the simulated `updated_at` of the change that opens it. A key's first version starts at its `created_at`.
2. Intervals are half-open, `[valid_from, valid_to)`. `valid_to` is the next version's `valid_from` in LSN order; the current version ends at `9999-12-31 00:00:00+00` and carries `is_current = true`. A point-in-time lookup is `t >= valid_from AND t < valid_to`.
3. Two changes of one key with the same `updated_at` and the same tracked values are copies (a re-snapshot), and the later LSN wins. With different values, they're a generator defect that the gold tests fail (INV-22); no macro breaks the tie.
4. Every timestamp is UTC `timestamptz`. A date is `(ts AT TIME ZONE 'UTC')::DATE`, so a session time zone never decides it. Keys hash `epoch_us(valid_from)`, never a timestamp cast to text.

### Generator clock rules

`analytics-eng` sets these rules, `platform` builds them into the generator, and [ADR-005](adr-005-generator-verification.md) names the property that tests each. D, L and K are knob defaults in ADR-005.

| # | Rule |
| - | ---- |
| C1 | One simulated clock, UTC, microsecond precision. It starts at a configured instant, only moves forward, and never reads the wall clock for a business time: no `now()`, no `CURRENT_TIMESTAMP`, no column `DEFAULT now()` in the generator's SQL or DDL. |
| C2 | One writer: the generator writes Postgres through one connection, one transaction at a time. Each transaction takes the next tick, strictly later than the previous transaction's, and every row it writes carries that tick as `updated_at`. Ticks therefore rise in commit order, and per key `updated_at` rises strictly in LSN order. |
| C3 | A key changes at most once per transaction, except the delete pair in C6. `created_at` is the insert's tick and never changes. |
| C4 | An event time equals the tick of the insert that records it, except under the late-order knob: an order's `ordered_at` lies in `[created_at − D, created_at]`, where D is that knob's maximum lateness. `paid_at` and a review's `created_at` always equal the tick. |
| C5 | Edit horizon L (L > D): the generator never inserts, updates or deletes an `orders` or `order_items` row whose `ordered_at` is more than L behind the clock. An order's status lifecycle completes within L of its `ordered_at`, so even a maximally late order can progress. A later refund is a new `payments` row and doesn't edit the order. |
| C6 | Business deletes follow the table below. Each hard delete is an `UPDATE` that sets `updated_at` to the tick, then the `DELETE`, in one transaction (ADR-001's delete pair). ADR-003's erasure deletes (Week 5) are the one exception: they remove a subject's rows unpaired, even a soft-deleted customer's. |
| C7 | Clickstream `event_ts` is set once, at the event's simulated time. The out-of-order and beyond-watermark knobs delay emission on the simulated clock and never change `event_ts`. A duplicate carries its original's `event_id` and `event_ts`. |
| C8 | The backfill covers `[2025-01-01T00:00Z, 2026-01-01T00:00Z)` by default. The live simulator continues from the backfill's end at a speed S times wall time, default S = 60, until the simulated clock reaches the wall clock; from then on it runs at 1×, so it never passes real ECB publications. The bounds and S are explicit config. The wall clock only paces; output depends on the simulated range alone. |
| C9 | The generator checkpoints its whole state (clock, every knob's RNG state, next ids, and the pending late products, delayed events and open order lifecycles) in Postgres in the same transaction as each tick's writes, in a table outside the CDC publication. A restart resumes from the checkpoint at its simulated time, never jumping by the downtime, and the backfill-to-live handoff is such a resume. Clickstream leaves the transaction, so each tick's events are emitted and flushed (idempotent producer from Week 6) before its checkpoint commits; a resume may re-send the last tick's events with the same `event_id` and `event_ts`, which `fct_page_views`' deduplication absorbs and the duplicate-rate checks exclude. A range ends with no flush: pending work stays in the checkpoint. |
| C10 | A backfill refuses to start unless the captured tables are empty and, from Week 8, Debezium's replication slot exists. From the week ADR-003's erasure ledger exists, the generator reads it at start and on every resume and emits nothing for a ledgered subject, matched as ADR-003 defines, and drops that subject's pending items from the checkpoint; `just erase` pauses the live simulator first, and `just erasure-proof` scans the checkpoint table. |

| Entity | Business delete | Change in the CDC stream | Effect in gold |
| ------ | --------------- | ------------------------ | -------------- |
| `customers` | Soft: sets `deleted_at` and `updated_at` to the tick; the customer then never changes again, until an erasure | `op=u` | A final `dim_customer` version with `is_deleted = true` |
| `products` | Soft (discontinued): sets `deleted_at` | `op=u` | `dim_product.is_deleted` overwritten (Type 1) |
| `orders` | Never; cancellation is a status change within L | `op=u` | Status attribute on `fct_orders` |
| `order_items` | Hard, only on an order still within L and with no capture yet; its `line_number` is never reused | Delete pair: `op=u`, then `op=d` | The line leaves `fct_order_items` |
| `payments` | Never; payments are insert-only, and a refund is a new row | `op=c` | A negative row in `fct_payments` |
| `reviews` | Hard (moderation), never the seeded injection review | Delete pair | The review leaves `fct_reviews` |

A soft delete isn't erasure: the row and its PII stay until ADR-002's retention or ADR-003's erasure removes them. Derivations read `after`, `op` and `source.*`, and a hard delete's time comes from its paired `op=u` row (same `source.txId`, lower LSN), so no derivation reads `before`. ADR-001's `REPLICA IDENTITY FULL` on the captured tables stays in force: it also stops Debezium from replacing an unchanged TOASTed column, such as a long review `body`, with its unavailable-value placeholder in `after`. Changing it is `platform`'s call in Week 8 and needs an ADR that answers both points.

### FX date rule

1. A money fact converts at the ECB rate of the latest publication on or before the event's UTC date: an equi-join to `fct_fx_rates_daily` on (`currency_code`, event UTC date), with `amount_eur = amount / rate` and EUR at 1.0 on every publication date ([bus matrix](../specs/transform/ref-bus-matrix.md)).
2. The FX data starts at least 4 days before the earliest event date, so the backfill's first day has a publication behind it. A currency's spine runs from its first loaded publication to the earlier of the global spine end and its own carry end: the last calendar day before the next TARGET business day after its last publication. A retired currency (BGN after 2025-12-31) therefore stops instead of carrying forward.
3. The global spine end is derived from the data, never from the build date: the last calendar day before the next TARGET business day after the newest loaded `rate_date`. `dim_date` flags TARGET business days.
4. A fact dated after its currency's spine end waits. Gold leaves it out until its rate lands, rather than converting it at an older rate or publishing a null EUR amount, so a published EUR amount is never restated. `platform`'s FX refresh (Airflow, Week 9) is what lands new rates; until then, facts past the offline data's end wait.
5. Reconciliation compares a control-totals snapshot of Postgres, taken at simulated time T with its commit LSN, against gold built once bronze holds every CDC change up to that LSN. It compares only event dates d with d + 1 day ≤ T − L and d no later than its currency's spine end; Week 11 builds the check.

### Consequences

- Good, because silver and every SCD2 dimension rebuild from bronze to the same rows whatever the arrival order or redelivery.
- Good, because validity and fact dates span simulated months while the stack runs for minutes.
- Good, because reconciliation gets a fixed, exclusive cutoff (C5, FX rule 5), and the generator's tests prove the source honours it.
- Good, because a restart or a second backfill can neither duplicate ids nor resurrect an erased subject (C9, C10).
- Bad, because one writer caps the CDC write rate. The Week 14 volume travels as clickstream, not CDC; the default backfill is sized to finish within 30 minutes on the laptop, and the generator's evidence records the rate one writer reaches.
- Bad, because gold lags the source until each day's rate lands: under a day normally, up to 4 days around Easter and Christmas, and indefinitely past the offline FX data until Week 9's refresh runs.
- Bad, because the checkpoint adds a generator-owned table and a write per tick.

### Confirmation

- **Week 3:** the generator's property tests ([ADR-005](adr-005-generator-verification.md) P2 to P5 and P12 to P14) check C1 to C10 on every CI run, the ledger half against a fake ledger, including a kill-and-resume run that equals an uninterrupted one.
- **Week 5:** with one entry in ADR-003's ledger, the generator emits 0 rows for that subject and holds none of its pending items.
- **Week 8:** ADR-001 item 11's bronze check, rerun on the real generator, finds 0 `updated_at` ties and 0 inversions per key in LSN order, and every `op=d` paired except erasure deletes.
- **Week 10:** INV-09's dbt unit tests apply out-of-order changes through the shared macro in `source.lsn` order.
- **Week 11:** INV-22's `non_overlapping_validity` test and a positive-length test fail on any version from a defect. `fct_fx_rates_daily` is unique on (`currency_code`, `rate_day`) with `days_carried ≤ 4`; a fact past its currency's spine end is absent and appears once its rate loads; INV-10's reconciliation runs at FX rule 5's cutoff.
- **Week 16:** an alert fires when any fact has waited for its rate longer than 4 days, beside INV-26.
- **Week 19:** game day #7 gains a case that erases a subject, runs a backfill again on a fresh database, and still finds 0 canary matches.
- **Review:** a model that orders changes by anything except `source.lsn`, or takes a date from the session time zone, fails review with this ADR cited.

## Pros and Cons of the Options

### LSN orders, simulated `updated_at` drives validity

- Good, because per-key LSN order equalled commit order in every one of item 11's 21,658 changes.
- Good, because validity carries business time across simulated months.
- Bad, because it needs generator rules (C2, C9) to keep the two clocks in step across restarts.

### Commit time (`source.ts_ms`) orders and drives validity

- Good, because Postgres sets it, so the generator owes nothing.
- Bad, because every change in a transaction shares it, so two changes of one row in one transaction tie.
- Bad, because a wall clock can step backwards, and every version would start within the minutes the run took, not on its business date.

### Simulated `updated_at` orders and drives validity

- Good, because one column does both jobs.
- Bad, because item 11's three writers inverted it twice; ordering by it would apply those changes in the wrong order.
- Bad, because a tie has no order at all, which ADR-001 forbids the macro to work around.

### `source.sequence` orders, simulated `updated_at` drives validity

- Good, because it's a global order across keys, and in item 11 it rose strictly per key (0 inversions in 12,700 comparisons).
- Bad, because it's a JSON array encoded as a string whose first element was null on 1 change and isn't a lower bound of the second under concurrent writers, and Debezium may change what that element holds.

## More Information

- Evidence: `docs/evidence/w2-spike.md` Item 11 (the two clocks and delete pairs) and Item 10 (270,096 ECB rows, 0 weekend rows, 134 gaps equal to the TARGET closing days).
- [ADR-001](adr-001-feasibility-spike.md) item 11 left these choices to this ADR. Its Delete validity section stands: the delete pair and `REPLICA IDENTITY FULL` both stay, and derivations take a delete's time from the pair's `op=u` row.
- [ADR-005](adr-005-generator-verification.md) holds the knob defaults (D, L, K) and the tests behind C1 to C10. The [bus matrix](../specs/transform/ref-bus-matrix.md) holds the grains, date roles and key names that apply these rules.
