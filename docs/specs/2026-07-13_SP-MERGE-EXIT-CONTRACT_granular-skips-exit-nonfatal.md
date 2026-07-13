---
id: SP-MERGE-EXIT-CONTRACT
title: review merge exit contract — every non-fatal skip exits 5
version: 0.1
status: draft
author: jfaye (PR #79 auto-merge failure triage, 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# review merge exit contract — every non-fatal skip exits 5

## Intent

`ferova review merge` documents a three-way exit contract — 0 merged
or already merged, 5 non-fatal gate skip, 1 fatal — but only honors it
for the three legacy skip outcomes. Every granular outcome added since
(`SKIP_CI_FAILED`, `SKIP_CI_TIMEOUT`, `SKIP_CI_MISSING`,
`SKIP_STALE_HEAD`) falls through to exit 1, turning designed-to-be-
non-fatal skips into red CI jobs. Close the mapping and guard it so a
future outcome constant can never silently regress to exit 1 again.

## Context

Observed live on PR #79 (run 29209292364, job "Auto-merge to develop",
2026-07-12): the CI gate correctly concluded `SKIP_CI_TIMEOUT` (the
Python 3.13 suite was still queued — see SP-AUTOMERGE-EVENT-DRIVEN for
that root cause), but the CLI exited 1, the workflow step (which
tolerates rc 0 and 5 only, `auto-review.yml:683-696`) failed the job
red, and the PR stalled ~22 h until a manual `gh run rerun --failed`.

- `src/ferova/review/auto_merge.py:77-86` defines the full outcome
  vocabulary: `OUTCOME_MERGED` ("APPROVE"), `OUTCOME_ALREADY_MERGED`,
  `OUTCOME_SKIP_BASE`, `OUTCOME_SKIP_GATE`, `OUTCOME_SKIP_CI`
  ("SKIP_CI_RED"), `OUTCOME_SKIP_CI_FAILED`, `OUTCOME_SKIP_CI_TIMEOUT`,
  `OUTCOME_SKIP_CI_MISSING`, `OUTCOME_SKIP_STALE_HEAD` (added by
  SP-AUTOMERGE-FRESH-HEAD), `OUTCOME_FAILED`.
- `src/ferova/cli/review_cmds.py:255-265` maps only
  `{OUTCOME_SKIP_BASE, OUTCOME_SKIP_CI, OUTCOME_SKIP_GATE}` to exit 5;
  everything unlisted hits the trailing `raise typer.Exit(code=1)`.
- The command docstring (`review_cmds.py:234-240`) already states the
  intended semantics: "5 — a non-merge gate prevented action …
  Non-fatal — the next review round can pick it up."

The defect class is a mapping maintained by hand in a different module
than the vocabulary it maps. The fix moves the classification next to
the constants and makes exhaustiveness a tested invariant.

Rollout coupling: this spec should land close to
SP-AUTOMERGE-EVENT-DRIVEN. In the window where only this spec is live,
a `SKIP_CI_TIMEOUT` no longer produces a red job (see A1): the stall
is visible only through the workflow's `::warning::` annotation and
the persisted `pr_merges` row, and recovery is the next PR event or
`scripts/safe_merge.sh` (a full `gh run rerun` also works;
`--failed` no longer has a failed job to target).

Same-file coordination: SP-CONSISTENCY-SWEEP C4 reworks the
`no_op_reason` substring exit maps elsewhere in `review_cmds.py`
(lines ~487-497, ~559-563) — disjoint from `review_merge`, no textual
conflict, but land the two sequentially to avoid rebase churn and keep
the shared principle (classification lives next to the vocabulary).

## Goals

- G1: `src/ferova/review/auto_merge.py` exports, adjacent to the
  outcome constants, two frozensets:
  `SUCCESS_OUTCOMES = frozenset({OUTCOME_MERGED, OUTCOME_ALREADY_MERGED})`
  and `NON_FATAL_SKIP_OUTCOMES = frozenset({OUTCOME_SKIP_BASE,
  OUTCOME_SKIP_GATE, OUTCOME_SKIP_CI, OUTCOME_SKIP_CI_FAILED,
  OUTCOME_SKIP_CI_TIMEOUT, OUTCOME_SKIP_CI_MISSING,
  OUTCOME_SKIP_STALE_HEAD})`. The existing inline success-set literal
  inside `run_auto_merge` (`auto_merge.py:757`,
  `if result.outcome in {OUTCOME_MERGED, OUTCOME_ALREADY_MERGED}`)
  is replaced by `SUCCESS_OUTCOMES` — pure refactor, same members.
- G2: a pure helper `merge_exit_code(outcome: str) -> int` in
  `auto_merge.py`: 0 for `SUCCESS_OUTCOMES`, 5 for
  `NON_FATAL_SKIP_OUTCOMES`, 1 for `OUTCOME_FAILED` and for ANY
  unrecognized string (fail-loud default). `auto_merge.py` must not
  import typer for this — the helper returns an int; raising the
  `typer.Exit` stays in the CLI layer.
- G3: `review_merge` (`src/ferova/cli/review_cmds.py`) derives its
  exit code exclusively from `merge_exit_code` — the local outcome
  sets and the hand-written if-chain are removed. The docstring
  enumerates the outcomes behind each exit code.
- G4: a completeness guard: a test reflects over every module
  attribute of `auto_merge` named `OUTCOME_*` and asserts its value is
  classified (member of `SUCCESS_OUTCOMES`, `NON_FATAL_SKIP_OUTCOMES`,
  or equal to `OUTCOME_FAILED`), so adding an outcome without
  classifying it fails the suite, not production.

## Non-Goals

- NG1: no change to `run_auto_merge` behavior, gate logic, or
  persisted outcome strings — this is exit-code plumbing only.
- NG2: no change to `.github/workflows/*` — the workflow's rc-0/5
  tolerance already matches the contract this spec restores
  (slot-holding and retriggering are SP-AUTOMERGE-EVENT-DRIVEN).
- NG3: no change to the exit codes of `review gate`, `review fix`, or
  the `release` commands.

## Assumptions

- A1: the only automated consumer of these exit codes is the
  auto-review workflow step, which treats 5 as success and 1 as
  failure. One HUMAN lever does rely on today's bug: a red auto-merge
  job is what `gh run rerun --failed` targets (the PR #79 recovery).
  This spec knowingly trades that accidental signal for contract
  correctness; SP-AUTOMERGE-EVENT-DRIVEN restores a real retrigger.
  The interim window is described in Context.
- A2: `AutoMergeResult.outcome` remains a plain string (persisted in
  `pr_merges`), so classification by string membership is stable.

## Interface

Inputs:
- `outcome`: `str` — an `AutoMergeResult.outcome` value.

Outputs:
- `merge_exit_code(outcome)`: `int` — process exit code per the table:

| outcome | exit |
| --- | --- |
| `APPROVE` (merged), `ALREADY_MERGED` | 0 |
| `SKIP_BASE`, `SKIP_GATE`, `SKIP_CI_RED`, `SKIP_CI_FAILED`, `SKIP_CI_TIMEOUT`, `SKIP_CI_MISSING`, `SKIP_STALE_HEAD` | 5 |
| `FAILED`, anything else | 1 |

Errors:
- none — the helper is total over `str`.

## Behavior

### Nominal

`review merge` prints the result JSON exactly as today, then exits
with `merge_exit_code(result.outcome)`.

### Edge cases

- Unknown outcome string (future constant, typo, corrupted row) →
  exit 1: loud, never silently non-fatal.

### Failure scenarios

- A new `OUTCOME_*` constant lands unclassified → the G4 guard test
  fails in the same PR that introduces it.

## Architecture Impact

- Adds/Removes dependency: none — the classification moves INTO the
  module that already owns the vocabulary; the CLI keeps its existing
  import edge onto `auto_merge`.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (single pure function, trivial flow).

## Acceptance Criteria

All new tests live in `tests/unit/test_review_merge_exit_contract.py`.

- [ ] AC1: `::test_every_outcome_constant_is_classified` — reflects
  over `OUTCOME_*` attributes of `ferova.review.auto_merge` and
  asserts each value is in `SUCCESS_OUTCOMES`, in
  `NON_FATAL_SKIP_OUTCOMES`, or equal to `OUTCOME_FAILED`.
- [ ] AC2: `::test_merge_exit_code_success_outcomes_zero` —
  parametrized over both success outcomes.
- [ ] AC3: `::test_merge_exit_code_non_fatal_skips_five` —
  parametrized over all seven skip outcomes, including
  `SKIP_CI_TIMEOUT` (the PR #79 case) and `SKIP_STALE_HEAD`.
- [ ] AC4: `::test_merge_exit_code_failed_and_unknown_one` — `FAILED`
  and an arbitrary unknown string both map to 1.
- [ ] AC5 (integration — CLI entry point):
  `::test_cli_review_merge_skip_ci_timeout_exits_five` and
  `::test_cli_review_merge_failed_exits_one` — invoke the `merge`
  command on `review_cmds.review_app` via `typer.testing.CliRunner`
  (the `tests/unit/test_review_lessons.py` /
  `tests/unit/test_planner_telemetry.py` invocation pattern) with
  `review_cmds.run_auto_merge` replaced at the CLI seam by a callable
  returning a genuine `AutoMergeResult` (the
  `tests/unit/test_release_cli.py` seam pattern), asserting
  `result.exit_code` and that the result JSON is still printed.
- [ ] AC6: existing suites stay green, in particular
  `tests/unit/test_review_auto_merge.py` and
  `tests/unit/test_review_merge_persistence.py` (no outcome string
  changes).

## Open Questions

None.
