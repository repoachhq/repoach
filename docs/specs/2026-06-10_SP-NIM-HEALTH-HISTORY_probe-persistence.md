# SP-NIM-HEALTH-HISTORY — persist each chain-health probe to SQLite

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-10

## Why

`ferova monitor-chains` (SP-NIM-CHAIN-HEALTH) probes each tier's NIM
head and classifies it, but the result is only printed + emitted as an
ephemeral structlog line. To analyse NIM volatility **across sessions**
(which models drift, when, latency distribution, failure rate) the
operator needs a durable, queryable time-series. The operator chose a
SQLite table sampled by a 15-minute systemd timer.

## What

Persist one row per probe to a `nim_health_probe` SQLite table in the
shared review DB (`get_settings().db_path`), and have `monitor-chains`
write to it on every run.

1. **`src/ferova/review/chain_health_store.py`** (new) — keeps the
   pure probe logic in `chain_health.py` free of any DB dependency:
   - A `nim_health_probe` table via its own `MetaData`:
     `id` (pk), `recorded_at` (DateTime tz, indexed), `tier`, `model`,
     `status`, `latency_s` (Float, nullable), `content_chars` (Integer),
     `detail` (String).
   - `init_nim_health_schema(db_path)` — `create_all(checkfirst=True)`,
     idempotent, mirrors `persistence.init_schema`.
   - `record_probes(db_path, probes, *, recorded_at)` — insert one row
     per `ModelHealth`; a single `recorded_at` stamps the whole sweep so
     rows from one run share a timestamp. Creates the schema first.
   - `ProbeRow` dataclass + `fetch_probes(db_path, *, since=None,
     tier=None, limit=None)` — read back, newest-first, for later
     analysis / a future summary command.
2. **`src/ferova/cli/main.py`** — after probing, `monitor-chains`
   calls `record_probes` unless `--no-persist` is passed. A
   `--db-path PATH` option overrides the default
   (`get_settings().db_path`). Persistence happens regardless of the
   exit code (a degraded sweep is exactly what we want recorded). One
   `nim_health_persisted` structlog line with the row count + db path.

The `datetime` stamp is taken once in the CLI (`datetime.now(UTC)`) and
passed into `record_probes`, so the store stays free of wall-clock reads
and is deterministic under test.

## Files in scope

- `src/ferova/review/chain_health_store.py` (new)
- `src/ferova/cli/main.py`
- `tests/unit/test_chain_health_store.py` (new)

## Out of scope

- The systemd timer that samples every 15 min (operator-installed ops
  step once this merges; not repo code).
- The passive proxy-log durability + enrichment (separate slice
  SP-NIM-LOG-DURABILITY).
- Any analysis / summary / dashboard command over the history (later).

## Smoke scenario

`record_probes` writes a sweep of three `ModelHealth` (ok / empty /
skipped) to a temp-file SQLite DB; `fetch_probes` returns them with the
shared `recorded_at` and correct fields. Invoking `monitor-chains`
through `CliRunner` with `check_tier_heads` patched to a scripted sweep
and `--db-path` pointing at a temp DB persists the rows; `--no-persist`
leaves the table empty.

## Definition of Done

- `record_probes` + `fetch_probes` round-trip a sweep, sharing one
  `recorded_at` — `test_record_and_fetch_round_trip`.
- `init_nim_health_schema` is idempotent (second call no-ops) —
  `test_schema_init_idempotent`.
- `fetch_probes(since=…)` filters by timestamp —
  `test_fetch_filters_since`.
- `monitor-chains --db-path <tmp>` persists every probed row —
  `test_cli_persists_probes`.
- `monitor-chains --no-persist` writes nothing —
  `test_cli_no_persist_skips`.
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(monitor): nim_health_probe SQLite store (record + fetch)`
2. `feat(cli): monitor-chains persists each sweep (--no-persist, --db-path)`
3. `test(monitor): store round-trip + schema idempotency + CLI persistence`

## Risks

- **Shared DB contention**: the 15-min timer and a concurrent review
  both open the same SQLite file; the existing `_engine_for` already
  sets `timeout=30, check_same_thread=False`, reused here.
- **Unbounded growth**: ~96 sweeps/day × 4 rows is tiny; no pruning for
  now, revisit if the table outgrows analysis convenience.
