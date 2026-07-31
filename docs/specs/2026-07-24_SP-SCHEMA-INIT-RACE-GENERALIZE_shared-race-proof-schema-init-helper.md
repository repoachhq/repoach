---
id: SP-SCHEMA-INIT-RACE-GENERALIZE
title: Generalize the race-proof schema-init fix to the other nine SQLite stores
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - src/repoach/core/sqlite_schema_init.py
    - src/repoach/review/planner_telemetry.py
    - src/repoach/review/stuck.py
  resources: N/A

depends_on:
  - SP-REVIEW-PERSIST-RECORDED-AT
  - SP-PLAN-SELECTOR-AUDIT-WIRE
  - SP-CHAINPILOT-AUDIT-LOG
  - SP-HEALTH-STORE-NEUTRALIZE
  - SP-CHAINPILOT-PROBE-SWEEP
  - SP-CHAINPILOT-EFFORT-SWEEP
  - SP-PROXY-STATE-PERSIST
provides_to: []

constraints: {}
---

# Generalize the race-proof schema-init fix to the other nine SQLite stores

## Intent

`SP-FINDINGS-INIT-RACE` diagnosed and fixed a real production race in
`findings.py`: two SQLite connections (same process or different
processes) racing the very first `CREATE TABLE` of a fresh database can
interleave, so the loser's `create_all(checkfirst=True)` raises
`OperationalError: table ... already exists` even though SQLite DDL is
transactional and never leaves a half-built table behind. That fix —
an in-process `threading.Lock` plus a bounded, convergent retry loop —
was never extracted into a shared helper, so it protects exactly one of
ten call sites. Extract it into one shared, metadata-generic helper and
wire all nine remaining stores through it.

## Context

`src/repoach/review/findings.py:185-261` (`_engine_for` +
`_INIT_SCHEMA_LOCK` + `_create_all_with_retries`, called from
`init_findings_schema`) is the only protected call site. The identical
unprotected two-line pattern — `engine = _engine_for(db_path)` /
`_metadata.create_all(engine, checkfirst=True)` — is independently
reimplemented, with no lock and no retry, at:

- `src/repoach/review/persistence.py:141-142` (`init_schema`, 5
  tables: `pr_reviews`, `pr_coder_responses`, `pr_hallucinations`,
  `pr_review_dialogue`, `pr_merges`) and again at
  `persistence.py:445-446` inside `fetch_review_dialogue`.
- `src/repoach/review/spec_gate.py:450-452`
  (`init_spec_coverage_schema`, table `pr_spec_coverage`).
- `src/repoach/review/audit_log.py:76-79` (`init_audit_schema`, table
  `chain_mutation_log`).
- `src/repoach/health/store.py:65-68` (`init_nim_health_schema`, table
  `nim_health_probe`) and again at `store.py:152-153` inside a fetch
  helper.
- `src/repoach/llm_proxy/providers/cell_probe_store.py:76-79`
  (`init_cell_health_schema`, table `cell_health_probe`) and again at
  `cell_probe_store.py:167-168`.
- `src/repoach/llm_proxy/providers/effort_probe_store.py:79-82`
  (`init_cell_effort_schema`, table `cell_effort_probe`) and again at
  `effort_probe_store.py:177-178`.
- `src/repoach/llm_proxy/routing/breaker_persist.py:72-75`
  (`init_breaker_state_schema`, table `breaker_trip_state`), and again
  at `breaker_persist.py:112-113` and `:183`.
- `src/repoach/review/planner_telemetry.py:76-77`
  (`init_planner_telemetry_schema`, table `planner_attempts`) — its own
  module docstring (lines 10-18) explicitly documents that it "follows
  the imperative SQLAlchemy Core scaffold of `findings` and
  `persistence` exactly", proving the duplication was copied forward
  deliberately rather than accidentally.
- `src/repoach/review/stuck.py:120-121` (`init_stuck_schema`, table
  `pr_coder_rounds`).

That is nine modules, fifteen unprotected call sites, none of them
covered by `SP-FINDINGS-INIT-RACE`'s test suite
(`tests/unit/test_findings_schema_race.py`,
`tests/integration/test_findings_schema_race_end_to_end.py` — both
scoped to `pr_findings` / `pr_review_integrity` only; the existing
end-to-end test's four-reviewer-thread race calls
`persistence.init_schema` exactly once, serially, from
`orchestrator.py:327` before the thread pool starts, so it never
exercises `persistence.py`'s own race window). Any real concurrent
first boot against a fresh `db_path` for any of these nine stores —
two proxy workers starting together, a fresh CI checkout invoking
`repoach` from two `pytest-xdist` workers, an operator script racing a
running service — still risks the same transient `OperationalError`.

`persistence.py`, `planner_telemetry.py`, and `stuck.py` carry no
governed-spec frontmatter (`src/repoach/arch/registry.py`'s
`_split_frontmatter` classifies any spec file that never opens a `---`
fence as a "frontier" node) — confirmed by grepping every
`docs/specs/*.md` frontmatter block for each path and finding no
`owns.code` entry; this spec claims them. `spec_gate.py`,
`audit_log.py`, `health/store.py`,
`llm_proxy/providers/cell_probe_store.py`,
`llm_proxy/providers/effort_probe_store.py`, and
`llm_proxy/routing/breaker_persist.py` are each already owned by an
existing governed spec (see `depends_on`); this spec edits them under
those declared coupling edges. `findings.py` itself is NOT touched —
it stays sole-owned by `SP-FINDINGS-INIT-RACE` (see Non-Goals).

## Goals

- G1: a new shared helper, `ensure_schema_created(engine, metadata)` in
  a new leaf module `src/repoach/core/sqlite_schema_init.py`,
  generalizes `findings.py`'s `_INIT_SCHEMA_LOCK` +
  `_create_all_with_retries` pattern to any SQLAlchemy `MetaData`
  (not hardcoded to `pr_findings` / `pr_review_integrity`): it
  serializes in-process callers on one lock, retries
  `metadata.create_all(engine, checkfirst=True)` up to a bounded
  attempt count on `OperationalError`, and re-raises only when at
  least one of `metadata.tables` genuinely never came to exist.
- G2: all fifteen unprotected call sites across the nine sibling
  modules listed in Context are replaced with a call to
  `ensure_schema_created(engine, _metadata)`, removing the raw
  `_metadata.create_all(engine, checkfirst=True)` two-liner from every
  one of them.
- G3: `findings.py` is left untouched — this spec does not introduce a
  second, competing implementation inside the file that already has
  the fix, and does not require an edit to `SP-FINDINGS-INIT-RACE`'s
  owned file to ship.
- G4: the observable schema-creation behavior of every one of the nine
  modules is unchanged in the nominal (uncontested) case — same
  tables, same columns, same idempotency; the only new observable
  effect is a `core.sqlite_schema_init.retry` log line on the rare
  occasions a race is actually hit, and the disappearance of the
  transient `OperationalError` that could previously surface from any
  of them.

## Non-Goals

- NG1: no change to `findings.py` — it keeps its own
  `_create_all_with_retries` implementation. A follow-up MAY point it
  at the shared helper later; that is out of scope here so this diff
  never touches a file owned by `SP-FINDINGS-INIT-RACE`.
- NG2: no change to any table's columns, any `_migrate_missing_columns`
  / ALTER-table self-heal logic, or any function's return type or
  signature — only the two-line engine-creation call immediately
  preceding those is replaced.
- NG3: no change to `breaker_persist.py`'s failure-swallowing contract
  (`write_through_trip_state` still never raises to its caller) — the
  shared helper's retry loop runs INSIDE that module's existing
  `try/except`, so an exhausted retry is still caught and logged there
  exactly as today.
- NG4: no cross-process file lock (e.g. `flock` on the `.db` file) —
  only an in-process `threading.Lock`, matching `findings.py`'s own
  scope; cross-process races remain handled by the bounded retry
  alone, unchanged in strategy from `findings.py`.
- NG5: no consolidation of the fifteen call sites' surrounding
  `_engine_for` helpers (each module keeps its own, since several
  differ in `connect_args`/`future=` kwargs) — only the
  `create_all(checkfirst=True)` step is shared.

## Assumptions

- A1: `OperationalError` is the only exception class a losing
  concurrent `CREATE TABLE` surfaces as (the same assumption
  `findings.py` already encodes) — the shared helper retries on that
  class only, exactly as the original.
- A2: `MetaData.create_all(engine, checkfirst=True)` is idempotent and
  convergent across retries for all nine schemas — each module's
  `Table` definitions are plain column declarations with no
  non-idempotent DDL (no triggers, no one-shot seed data), verified by
  inspection of each `Table(...)` block in Context.

## Interface

New module `src/repoach/core/sqlite_schema_init.py`:

```python
def ensure_schema_created(engine: Engine, metadata: MetaData) -> None:
    """Create every table in *metadata* against *engine*, race-proof.

    Generalizes the fix ``SP-FINDINGS-INIT-RACE`` shipped for
    ``pr_findings`` / ``pr_review_integrity`` to any SQLAlchemy
    ``MetaData``: an in-process lock serializes concurrent
    first-creation within one process, and a bounded retry loop
    absorbs the ``OperationalError`` a losing cross-process
    ``CREATE TABLE`` surfaces as, since SQLite DDL is transactional
    and never leaves a half-built table behind.

    Args:
        engine: SQLAlchemy engine bound to the target database.
        metadata: The ``MetaData`` whose declared tables must exist.

    Raises:
        OperationalError: The database remains unusable after every
            retry -- at least one declared table still absent.
    """
```

`src/repoach/review/persistence.py`, `spec_gate.py`, `audit_log.py`,
`src/repoach/health/store.py`,
`src/repoach/llm_proxy/providers/cell_probe_store.py`,
`src/repoach/llm_proxy/providers/effort_probe_store.py`,
`src/repoach/llm_proxy/routing/breaker_persist.py`,
`src/repoach/review/planner_telemetry.py`, `stuck.py`: each adds
`from ...core.sqlite_schema_init import ensure_schema_created` (relative
depth per module) and replaces every

```python
engine = _engine_for(db_path)
_metadata.create_all(engine, checkfirst=True)
```

with

```python
engine = _engine_for(db_path)
ensure_schema_created(engine, _metadata)
```

(the `spec_gate.py` one-liner `_metadata.create_all(_engine_for(db_path),
checkfirst=True)` becomes `ensure_schema_created(_engine_for(db_path),
_metadata)`).

## Behavior

### Nominal

A single-process, single-caller boot of any of the nine stores calls
`ensure_schema_created`, which acquires the (uncontended) lock, creates
every missing table in one pass, releases the lock, and returns —
identical observable behavior to today's raw `create_all` call.

### Edge cases

- Two threads in the SAME process call `ensure_schema_created` for the
  SAME fresh `db_path`/`metadata` concurrently -> the loser blocks on
  the lock, then finds every table already present via `checkfirst`
  and no-ops (the in-process half of the fix, now available to all
  nine siblings).
- Two separate PROCESSES race the very first `CREATE TABLE` for the
  same store -> the loser's `create_all` raises `OperationalError`;
  the bounded retry (`checkfirst=True` re-entered) converges once the
  winner's commit becomes visible, without ever propagating the
  transient error.

### Failure scenarios

- After the bounded attempt count, if the module's declared tables are
  STILL not all present (a genuine failure — e.g. an unwritable
  database file) -> `OperationalError` is re-raised to the caller,
  matching `findings.py`'s existing failure contract exactly;
  `breaker_persist.py`'s caller-side `try/except` still swallows it as
  today (NG3).

## Architecture Impact

- Adds one new leaf module, `src/repoach/core/sqlite_schema_init.py`
  (`owns.code` on this spec), imported by nine already-existing
  modules across `review/`, `llm_proxy/providers/`,
  `llm_proxy/routing/`, and `health/` — mirrors the existing
  `core.config` / `core.logging` leaf-import pattern; `core/` imports
  no governed component, so this introduces no cycle.
- Cross-owner edits authorized by `depends_on`: `spec_gate.py`
  (`SP-PLAN-SELECTOR-AUDIT-WIRE`), `audit_log.py`
  (`SP-CHAINPILOT-AUDIT-LOG`), `health/store.py`
  (`SP-HEALTH-STORE-NEUTRALIZE`), `cell_probe_store.py`
  (`SP-CHAINPILOT-PROBE-SWEEP`), `effort_probe_store.py`
  (`SP-CHAINPILOT-EFFORT-SWEEP`), `breaker_persist.py`
  (`SP-PROXY-STATE-PERSIST`) each gain exactly one new import line and
  one one-line call-site swap per existing `create_all` occurrence —
  no other change to those files.
- `persistence.py`, `planner_telemetry.py`, `stuck.py` are frontier
  (unowned) nodes today; this spec claims them in `owns.code` — the
  minimal edit each receives is the same import + call-site swap.
- `findings.py` is untouched; no edge to `SP-FINDINGS-INIT-RACE`'s
  file is introduced, only a documentation cross-reference in Context.
- New / changed coupling: nine previously-independent hand-rolled
  implementations collapse onto one shared helper — net coupling
  DECREASES (dedup); no new cycle since `core/` is a leaf.

## Diagram

N/A (mechanical extraction + call-site swap across existing modules).

## Acceptance Criteria

- [ ] AC1: unit — the shared helper's retry/reraise contract, generic
  over an arbitrary `MetaData` (not hardcoded to any one schema):
  `test_ensure_schema_created_converges_after_transient_operational_errors`
  (a fake `MetaData`-like stub whose `create_all` raises
  `OperationalError` twice then delegates to the real
  `MetaData.create_all`, mirroring
  `test_findings_schema_race.py`'s `_fake_create_all` technique;
  asserts `ensure_schema_created` returns without raising and the
  underlying tables exist) and
  `test_ensure_schema_created_reraises_after_exhausting_retries` (a
  stub whose `create_all` always raises; asserts the `OperationalError`
  propagates once the bounded attempt count is exhausted). Both fail
  today with `ModuleNotFoundError` / `ImportError` — the module does
  not exist yet.
- [ ] AC2: unit — breadth of adoption across all nine siblings, one
  test asserting every sibling now routes through the shared helper
  rather than a private re-implementation:
  `test_sqlite_schema_init_adoption.py::test_all_nine_sibling_stores_call_the_shared_helper`.
  For each of the nine `(module, init_function, db_path_factory)`
  tuples (`persistence.init_schema`, `spec_gate.init_spec_coverage_schema`,
  `audit_log.init_audit_schema`, `store.init_nim_health_schema`,
  `cell_probe_store.init_cell_health_schema`,
  `effort_probe_store.init_cell_effort_schema`,
  `breaker_persist.init_breaker_state_schema`,
  `planner_telemetry.init_planner_telemetry_schema`,
  `stuck.init_stuck_schema`), monkeypatches that module's imported
  `ensure_schema_created` binding with a call-recording spy, invokes
  the init function against a fresh `tmp_path` database, and asserts
  the spy was called exactly once. Fails today for every one of the
  nine (`monkeypatch.setattr` raises `AttributeError` — the name is
  not bound in any of these modules yet); passes once each module
  imports and calls it.
- [ ] AC3 (INTEGRATION): a real concurrency reproduction on a
  previously-unprotected sibling, mirroring
  `test_findings_schema_race.py`'s own barrier-based technique:
  `tests/integration/test_stuck_schema_race.py::test_concurrent_init_stuck_schema_no_operational_error`.
  Eight threads call `stuck.init_stuck_schema(db_path)` against the
  same fresh `tmp_path`, released simultaneously via a
  `threading.Barrier`; asserts zero exceptions across all eight
  threads and that `pr_coder_rounds` exists afterward. On today's
  unmodified `stuck.py` this reliably surfaces
  `OperationalError: table pr_coder_rounds already exists` in at least
  one thread (the same class of failure `findings.py`'s own
  now-fixed test, `test_concurrent_init_findings_schema_no_operational_error`,
  reproduced pre-fix); after wiring `stuck.py` through
  `ensure_schema_created` it passes cleanly.
- [ ] AC4: promised test files —
  `tests/unit/test_sqlite_schema_init.py::test_ensure_schema_created_converges_after_transient_operational_errors`,
  `tests/unit/test_sqlite_schema_init.py::test_ensure_schema_created_reraises_after_exhausting_retries`,
  `tests/unit/test_sqlite_schema_init_adoption.py::test_all_nine_sibling_stores_call_the_shared_helper`,
  `tests/integration/test_stuck_schema_race.py::test_concurrent_init_stuck_schema_no_operational_error`.
- [ ] AC5: `ruff check` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa` anywhere in the diff; `repoach arch graph --check` exits 0.

## Open Questions

(none)
