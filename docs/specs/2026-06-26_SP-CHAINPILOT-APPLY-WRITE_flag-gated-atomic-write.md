---
id: SP-CHAINPILOT-APPLY-WRITE
title: Flag-gated atomic write of a planned chains.env rewrite
version: 0.1
status: draft
author: agent
created: 2026-06-26
updated: 2026-06-26

owns:
  code: src/repoach/review/chain_apply.py
  resources: N/A

depends_on:
  - SP-CHAINPILOT-PLAN       # the ChainRewritePlan it consumes (3d-1c)
  - SP-CHAINPILOT-AUDIT-LOG  # record_mutations journal (3c)
  - SP-CHAINPILOT-DECISION   # PlannedMutation (3b)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-LOOP 3e)
constraints: {}
---

# SP-CHAINPILOT-APPLY-WRITE — the one slice that writes chains.env

## Intent
Phase 3d-2 — the only slice that mutates the authoritative `chains.env`, and only
behind a flag (`enabled`, OFF by default). Given a `ChainRewritePlan` (3d-1c) it
journals the planned mutations + cold-starts to the 3c audit log and, when
enabled and the content actually changed, backs up the current file and writes
the new content atomically. It adds no policy and no structural reasoning — those
are 3b / 3d-1c / 3d-1a; this is the careful hand on the live file.

## Context
Two safety properties matter for touching the live config the proxy and CI read:
- **Flag-gated.** Nothing is written unless the caller passes `enabled=True`
  (3e reads it from settings); the default path is a shadow journal, so the whole
  loop can run in production recording what it *would* do before anyone arms it.
- **Atomic + reversible.** The write goes to a temp file in the same directory
  then `os.replace` (atomic on POSIX), so a crash never leaves a half-written
  chain config; the prior version is copied to `chains.env.bak` first, so a bad
  automatic change is one restore away.

The journal's `applied` is per-row and truthful: a mutation is `applied=True`
only when this run wrote AND that edit actually landed (in `plan.rewrite.applied`)
— so an `advise`, a refused edit, or any row in a shadow / no-change run is
`applied=False`, and a future loop reading the ledger never sees a change that
did not happen.

## Goals
- G1: A new leaf `src/ferova/review/chain_apply.py` — its only I/O is the
  `chains.env` read/backup/write and the audit-log record (no network, no
  decisions).
- G2: `ApplyResult(written, backup_path, journaled)`.
- G3: `apply_chain_rewrite(plan, mutations, *, db_path, chains_path, recorded_at,
  enabled=False) -> ApplyResult` — journal `mutations + plan.cold_starts` each
  with its **true per-row** `applied`: a row is `applied=True` only when this run
  wrote AND its edit is in `plan.rewrite.applied`; an `advise` (no edit) or a
  refused edit is `applied=False`. When `enabled` and `new_content != current`,
  back up then atomically write.
- G4: The write stages the new content to a temp file, copies the current file to
  `.bak`, preserves the original file mode, then `os.replace`s; a failed write
  cleans up its temp, journals the attempt as not-applied (never silent) and
  re-raises, leaving the original intact.

## Non-Goals
- NG1: Does NOT decide what to write (3b/3d-1c) nor enforce chain structure
  (3d-1a) — it trusts `plan.rewrite.new_content`.
- NG2: Does NOT read the flag from settings, sweep, attribute or schedule — 3e
  gathers the inputs and supplies `enabled`.
- NG3: Does NOT auto-rollback — the `.bak` is the manual restore path; the
  atomic write means failure leaves the original intact.

## Assumptions
- A1: `plan.rewrite.new_content` was computed from the same `chains.env` (3d-1c
  read the current content), so a plain content compare detects a real change.
- A2: `os.replace` within one directory is atomic on the target platform (POSIX).
- A3: The audit log's `applied` is per-row (MutationRecord documents it "True
  when written to chains.env"), so this slice splits the batch into an
  applied-true set (landed edits + cold-starts on a real write) and an
  applied-false set (advise / refused / shadow).

## Interface
New (all in `chain_apply.py`): `ApplyResult`, `apply_chain_rewrite`.

## Behavior

### Nominal
- `enabled=False` (shadow): no write, no `.bak`, journal rows with
  `applied=False`; `written=False`.
- `enabled=True` and content changed: `.bak` holds the old content, `chains.env`
  holds `new_content`, journal `applied=True`; `written=True`.
- Journal count == `len(mutations) + len(plan.cold_starts)`.

### Edge cases
- `enabled=True` but `new_content == current` → no write, no `.bak`,
  `applied=False`.
- Empty `mutations` and no cold-starts → nothing journalled (0 rows), no write.
- Missing `chains.env` → no write (nothing to back up), shadow journal only.

### Failure scenarios
- A write error unlinks the temp file and re-raises (no partial file, no silent
  swallow); the original `chains.env` is untouched (replace not yet done).

## Architecture Impact
- New leaf in `review/` importing `ChainRewritePlan` (3d-1c), `record_mutations`
  (3c), `PlannedMutation` (3b) — all in-package, governed edges declared. No
  cycle.
- Nobody imports it yet (3e will), so per
  [[unwired-invariant-breaks-next-slice]] no "nothing imports me" assertion is
  pinned and the FULL unit suite is run.

## Acceptance Criteria
- [ ] AC1: A shadow run (`enabled=False`) leaves `chains.env` byte-identical,
  writes no `.bak`, and journals every mutation + cold-start with
  `applied=False`; `written=False`.
- [ ] AC2: An enabled run with changed content writes `new_content` atomically,
  copies the old content to `chains.env.bak`, journals with `applied=True`, and
  reports `written=True`.
- [ ] AC3: An enabled run whose `new_content` equals the current file makes no
  write, no `.bak`, journals `applied=False`.
- [ ] AC4: `journaled == len(mutations) + len(plan.cold_starts)`; on a real write
  a landed edit + a cold-start are `applied=True` while an `advise` (and any
  refused edit) is `applied=False`.
- [ ] AC5: A missing `chains.env` yields a shadow journal and no write; an
  enabled write preserves the original file mode.
- [ ] AC6: ruff + format + no-inline + no-silent-except + `arch check` pass;
  mypy-strict clean on the module; full `pytest tests/unit` green.

## Open Questions
- None. (The settings flag name + the cadence that supplies `enabled` are 3e.)
