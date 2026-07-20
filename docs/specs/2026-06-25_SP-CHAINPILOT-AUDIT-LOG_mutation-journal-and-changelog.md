---
id: SP-CHAINPILOT-AUDIT-LOG
title: Mutation journal — durable record + human-readable changelog
version: 0.1
status: draft
author: agent
created: 2026-06-25
updated: 2026-06-25

owns:
  code: src/repoach/review/audit_log.py
  resources: db:table:chain_mutation_log

depends_on:
  - SP-CHAINPILOT-DECISION          # PlannedMutation / MutationKind (3b)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-APPLY, 3d)
constraints: {}
---

# SP-CHAINPILOT-AUDIT-LOG — never lose the why

## Intent
Phase 3c of the Chain Autopilot arc — the "documented" half of Principle 7
(*automatic but documented*). It persists every chain mutation the engine
emits, with what changed, the signal that triggered it, when, and free-text
context (what it replaced) — a queryable table plus a human-readable changelog
renderer. So a mutation is never silent and its rationale is never lost.

## Context
3b (`plan_mutations`) produces `PlannedMutation`s; 3d will apply them. This
slice is the journal both sides write to. It carries an `applied` flag so a
**shadow / dry-run** run (3d proposing without writing `chains.env`) is
journaled with `applied = False`, while a real apply is `applied = True` — the
same ledger records both, which is how we build confidence before letting the
loop touch the live file.

It mirrors the arc's existing persistence leaves (`cell_probe_store`,
`effort_probe_store`): a single SQLite table, idempotent `init`, a batch
`record`, a filtered `fetch`, with `recorded_at` injected so a run's rows share
one timestamp and tests stay deterministic. It lives in `review/` because it
imports `PlannedMutation` from `review/decision.py`; the `render_changelog`
function is pure.

## Goals
- G1: A new leaf `src/ferova/review/audit_log.py` owning a new
  `chain_mutation_log` table.
- G2: `init_audit_schema(db_path)` — idempotent table creation.
- G3: `record_mutations(db_path, mutations, *, applied, recorded_at, detail="")`
  — persist a batch of `PlannedMutation`, every row sharing `recorded_at`,
  stamped with `applied` and an optional `detail` (the what-it-replaced
  context the caller supplies).
- G4: A frozen `MutationRecord` (the row read back: `recorded_at`, `kind`,
  `model`, `provider`, `metric`, `reason`, `applied`, `detail`) and
  `fetch_mutations(db_path, *, since=None, limit=None) -> list[MutationRecord]`,
  newest-first.
- G5: `render_changelog(records) -> str` — a pure, human-readable rendering
  (one line per record: timestamp, APPLIED/SHADOW, kind, target, reason).

## Non-Goals
- NG1: Does NOT decide or apply mutations (3b plans, 3d applies); it only
  records and renders.
- NG2: Does NOT write `chains.env` or compute the before/after diff — the
  `detail` field carries whatever context the caller (3d) provides.
- NG3: Does NOT mutate or read the network; pure SQLite + string rendering.

## Assumptions
- A1: `PlannedMutation` fields (`kind`, `model`, `provider`, `metric`,
  `reason`) map directly to columns; `provider` / `metric` are nullable.
- A2: `recorded_at` is injected (UTC), mirroring the sibling stores, so a
  single run's rows are grouped and tests are deterministic.

## Interface
New (all in `audit_log.py`):
- `chain_mutation_log` table; `init_audit_schema`.
- `@dataclass(frozen=True) class MutationRecord`.
- `record_mutations(...)`, `fetch_mutations(...)`, `render_changelog(...)`.

## Behavior

### Nominal
- Recording 3 mutations with `applied=True` then `fetch_mutations` returns 3
  `MutationRecord`s newest-first, each carrying its kind/model/reason.
- `render_changelog` of those yields 3 lines, each tagged `APPLIED`.

### Edge cases
- A shadow run records with `applied=False` → rows tagged `SHADOW`; both shadow
  and applied rows coexist in the ledger.
- `fetch_mutations(since=...)` filters by timestamp; `limit` caps the count.
- An empty batch records nothing; `render_changelog([])` is the empty string.
- A `drop_provider` row round-trips its `provider`; a `promote` its `metric`.

### Failure scenarios
- Fetching from a fresh DB creates the table and returns `[]` (idempotent
  init), never raising.

## Architecture Impact
- New leaf in `review/`; owns the `db:table:chain_mutation_log` resource;
  imports `PlannedMutation` from 3b (declared edge). No cycle.
- Mirrors the sibling stores' shape; nobody imports it yet (3d will), so per
  [[unwired-invariant-breaks-next-slice]] the FULL unit suite is run and no
  "nothing imports me" assertion is pinned.

## Acceptance Criteria
- [ ] AC1: `record_mutations` persists each `PlannedMutation` with its
  `recorded_at`, `applied`, and `detail`; `provider` / `metric` round-trip
  including `None`.
- [ ] AC2: `fetch_mutations` returns `MutationRecord`s newest-first; `since`
  and `limit` filter as documented.
- [ ] AC3: An empty batch records nothing and raises nothing.
- [ ] AC4: `applied=False` rows are tagged distinctly from `applied=True` rows
  in both storage and `render_changelog`.
- [ ] AC5: `render_changelog([])` is `""`; a non-empty render has one line per
  record naming kind, target, and reason.
- [ ] AC6: A fresh DB path yields `[]` without raising.
- [ ] AC7: `arch check` passes with the declared DECISION edge + the owned
  table resource; ruff + no-inline pass.

## Open Questions
- None.
