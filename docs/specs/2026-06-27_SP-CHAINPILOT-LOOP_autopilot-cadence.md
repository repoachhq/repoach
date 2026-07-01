---
id: SP-CHAINPILOT-LOOP
title: Autopilot cadence — sweep → attribute → decide → plan → apply
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: src/ferova/review/chain_loop.py
  resources: N/A

depends_on:
  - SP-CHAINPILOT-MATRIX             # sweep_model_matrix (1b)
  - SP-CHAINPILOT-PROBE-SWEEP        # sweep_cell_health + cell_probe_store (2a-2)
  - SP-CHAINPILOT-ATTRIBUTION        # attribute_faults + summarize_cells (3a)
  - SP-CHAINPILOT-PERF-AGGREGATE     # aggregate_model_performance (2d)
  - SP-CHAINPILOT-DECISION           # plan_mutations (3b)
  - SP-CHAINPILOT-PLAN               # plan_chain_rewrite (3d-1c)
  - SP-CHAINPILOT-APPLY-WRITE        # apply_chain_rewrite (3d-2)
  - SP-CHAINPILOT-BENCHMARK-INGEST   # BenchmarkRanking (1c/3d-0)
  - SP-CHAINPILOT-EQUIVALENCES       # EquivalenceTable (1d)

provides_to: []                  # the loop is the top of the arc — a routine/cron calls it
constraints: {}
---

# SP-CHAINPILOT-LOOP — the arc becomes a loop

## Intent
Phase 3e — the slice that finally turns the Chain Autopilot into a self-running
loop. One cycle sweeps the live `(provider × model)` matrix and probes every
cell (1b/2a), attributes faults (3a), scores the models (2d), decides mutations
(3b), plans the shadow rewrite (3d-1c) and applies it behind the flag (3d-2).
Everything it composes is already built, tested and shadow-run; this is the
wiring plus the `ferova autopilot` entry point a routine/cron calls.

## Context
The cycle is split so its decision half is testable without a network:
- `run_autopilot_cycle` (async) does the I/O — `sweep_model_matrix` +
  `sweep_cell_health` + `record_cell_probes` — then hands off;
- `plan_and_apply` (sync) is network-free — it reads the recent probes, runs
  attribution → scoreboard → `plan_mutations` → `plan_chain_rewrite` →
  `apply_chain_rewrite`, all on an injected matrix + the probe DB;
- `select_healthy_cells` is pure — the `(provider, model)` cells that clear the
  3a ok-share bar, so cold-start provider resolution prefers observed-working
  cells.

Two freshness safeguards keep an autonomous, self-modifying config honest:
- **Rolling window.** Attribution + healthy-cell selection read only probes in
  the last `lookback` (default 24h, `since = recorded_at - lookback`), so a model
  that dies *this* cycle is not masked by lifetime-aggregated ok probes and a
  long-ago sick model is not held against forever (Principle 5).
- **Fresh-observation gate.** `run_autopilot_cycle` arms the write only when the
  sweep actually recorded probes this cycle (`recorded > 0`); an offline /
  credential-less cycle that observed nothing degrades to a shadow run instead of
  evicting models from stale data.

The write stays gated by `enabled` (3d-2): the default run is a **shadow** that
journals what it *would* do, so a routine can run the whole loop in production
until the operator arms it via `FEROVA_CHAINPILOT_APPLY_ENABLED` (a new settings
flag, default `False`) or the `--apply` CLI flag.

## Goals
- G1: A new leaf `src/ferova/review/chain_loop.py`: `select_healthy_cells`
  (pure), `plan_and_apply` (sync core), `run_autopilot_cycle` (async cadence),
  `CycleReport`.
- G2: A `chainpilot_apply_enabled` settings flag (`FEROVA_*`, default `False`).
- G3: A `ferova autopilot` CLI command mirroring `monitor-chains`: build the
  client + settings + ranking + equivalences, run one cycle, log + echo the
  `CycleReport`; `--apply` (or the flag) arms the write.
- G4: One cycle composes the pipeline end to end with no new policy — the loop
  only sequences and gathers the live inputs.

## Non-Goals
- NG1: No scheduling/cron in this slice — it exposes a single-cycle entry point;
  a routine/timer invokes it (like the nim_health timer).
- NG2: No new decision, placement, write or safety logic — those are 3a–3d.
- NG3: Does not change `chains.env` unless armed (`enabled`); the default is a
  shadow journal.

## Assumptions
- A1: The sweeps never raise (1b/2a), so a cycle degrades to an empty/partial
  matrix rather than crashing the routine.
- A2: An empty matrix yields no cold-start (resolution needs a cell); fault /
  quality mutations still derive from the recent probe window, so the offline
  safety comes from the fresh-observation gate (no write when nothing was swept),
  not from the empty matrix alone.
- A3: `recorded_at` is injected once per cycle so the probe rows and the journal
  rows share a timestamp.

## Interface
New (in `chain_loop.py`): `CycleReport`, `select_healthy_cells`, `plan_and_apply`,
`run_autopilot_cycle`. Amends `settings.py` (+`chainpilot_apply_enabled`) and
`cli/main.py` (+`autopilot` command).

## Behavior

### Nominal
- A healthy, benchmark-known model absent from the chains and present in the live
  matrix is cold-started into its placed tier; on an armed run it is written +
  journalled `applied=True`, on a shadow run journalled `applied=False`.
- A fault / quality mutation from 3b flows through to the rewrite.
- The `CycleReport` summarises cells / faults / mutations / cold-starts / written
  / journaled.

### Edge cases
- Empty matrix (offline / no creds) → no cold-starts, no write, shadow journal.
- `enabled=False` (default) → never writes.
- A cell below the sample / ok-share bar is not in `select_healthy_cells`.

### Failure scenarios
- The sweeps absorb provider errors (never raise); the synchronous half is
  network-free, so a cycle either completes or raises only on a genuinely broken
  DB / file (surfaced to the routine).

## Architecture Impact
- New leaf in `review/` importing the sweep/attribution/matrix modules from
  `llm_proxy` (the safe `review → llm_proxy` direction) and the in-package 2d/3b/
  3d leaves — all governed edges declared in `depends_on`. AMENDS `settings.py`
  (a flag) and `cli/main.py` (a command). No cycle.
- The CLI command is thin glue over the tested core; the pure + sync halves carry
  the logic and the tests.

## Acceptance Criteria
- [ ] AC1: `select_healthy_cells` returns exactly the cells clearing the
  min-samples + ok-fraction bar.
- [ ] AC2: `plan_and_apply` with a healthy, absent, matrix-present model cold-starts
  it into its placed tier and writes when `enabled=True`.
- [ ] AC3: The same with `enabled=False` leaves `chains.env` byte-identical
  (shadow).
- [ ] AC4: An empty matrix yields no cold-start; attribution/healthy-cell read
  only the `lookback` window (stale probes are excluded — a model is evicted on
  *recent* faults, not lifetime-aggregated ones).
- [ ] AC5: `run_autopilot_cycle` runs end to end offline (fake client → empty
  matrix) and returns `written=False`; and it never writes when the sweep
  recorded no probes this cycle, even with recent fault probes in the DB.
- [ ] AC6: `chainpilot_apply_enabled` is a `FEROVA_*` flag defaulting to `False`
  (the alias-map tests stay green).
- [ ] AC7: ruff + format + no-inline + no-silent-except + `arch check` pass;
  mypy-strict clean on `chain_loop.py`; full `pytest tests/unit` green.

## Open Questions
- The scheduling cadence (a routine/cron interval) and whether to wire a push
  notification on an armed write are a follow-up once the loop has shadow-run in
  production for a while.
