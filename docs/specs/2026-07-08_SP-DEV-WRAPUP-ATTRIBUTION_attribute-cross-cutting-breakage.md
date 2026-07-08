---
id: SP-DEV-WRAPUP-ATTRIBUTION
title: Wrap-up attributes cross-cutting breakage to its introducing step and repairs it
version: 0.1
status: approved
author: jfaye (three live incidents 2026-07-07/08; operator GO 2026-07-08)
created: 2026-07-08
updated: 2026-07-08

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Wrap-up attributes cross-cutting breakage to its introducing step and repairs it

## Intent

When the wrap-up full suite is red on tests OUTSIDE the plan's
promised selectors, the session must not shrug (`fixes_applied: 0`,
refuse push) — it must find WHICH step commit introduced each
failure, hand that step's context to the loop for one bounded repair
attempt, and, if repair fails, produce a dossier that NAMES the
introducing step and diff instead of an unattributed red suite.

## Context

Step gates only run the step's promised selectors, so a step can
break a test it never promised and every gate stays green until the
wrap-up full suite. Three live occurrences in 24 h (2026-07-07/08):

1. SP-REFUTED-FEEDBACK step 3 broke 12 reviewer-stub tests
   (`db_path` kwarg) — resumed session no-oped
   (`fixes_applied: 0`), operator hand repair 87b0855.
2. SP-CHAIN-DEAD-HOP-QUARANTINE step 2 broke the settings-alias
   exhaustiveness test — same no-op shape, hand repair f1c2fe3.
3. The original pause-point incident (2026-07-06) — same class.

Each time the machinery to FIX the failure existed (the loop repairs
promised-test failures fine); what was missing is attribution: no
step owned the breakage, so no repair brief was ever assembled.

## Goals

- G1: A pure-orchestration helper
  `attribute_failure_to_step(selector, step_commits, *, run_selector)`
  in a new `src/ferova/review/wrapup_attribution.py`: given the
  session's ordered step commits and an injectable selector runner,
  it identifies the first commit at which the selector fails
  (checking the plan base first, so pre-existing failures are
  reported as `pre_existing`, never blamed on a step). Linear walk is
  acceptable (plans are ≤ ~6 steps).
- G2: The wrap-up path in `dev_runner` uses it: for each failing
  selector outside the promised set, attribution runs in a temporary
  worktree (never disturbing the session checkout), then ONE bounded
  repair attempt per introducing step is dispatched to the existing
  fix loop with a brief carrying: the failing selector(s), the
  introducing step's title, files and diff. Bounded by a module
  constant `WRAPUP_REPAIR_ATTEMPTS = 1`.
- G3: Outcomes are truthful: a successful repair lands as a commit
  `fix(wrapup): <selector> broken by step <n>` and the wrap-up suite
  re-runs; a failed repair keeps today's refuse-to-push behaviour but
  the session result and the stuck dossier now carry the attribution
  (step index, title, selector, diff stat) — `no_op_reason` names the
  step, never just "full unit suite pytest red".
- G4: Pre-existing failures (red at the plan base) are surfaced as
  such in the session result and are NOT repaired by the session
  (they predate it) — the dossier lists them under `pre_existing`.

## Non-Goals

- NG1: No change to step gates (they still run only promised
  selectors — cheap steps stay cheap; the wrap-up is the safety net).
- NG2: No bisect optimisation, no parallel attribution — linear over
  ≤ ~6 commits in one worktree is enough.
- NG3: No attempt to repair `pre_existing` failures.

## Assumptions

- A1: Session step commits are linear on the impl branch (one commit
  per step — the existing convention) and reachable from the wrap-up
  head.
- A2: A temporary `git worktree` is available on both the operator
  clone and the CI runner (same git everywhere).
- A3: The existing fix loop accepts a scoped brief (it already
  consumes step briefs; the repair brief reuses that shape).

## Behavior

### Nominal

Wrap-up green → nothing changes. Wrap-up red only on promised
selectors → existing behaviour (the loop already owns those).

### The new path

Wrap-up red on `tests/unit/test_x.py::test_y` (not promised by any
step): attribution walks base, c1, c2, … in a temp worktree running
only that selector; first red commit = introducing step. The loop
gets one repair attempt with that step's context; on green re-run of
the failing selectors + full suite, the session proceeds to
self-verify and push as usual. On failure, refuse-to-push with the
attributed dossier.

### Edge cases

- Selector red at the plan base → `pre_existing`, reported, skipped.
- Two selectors broken by different steps → one repair brief per
  introducing step, still `WRAPUP_REPAIR_ATTEMPTS` per step.
- Attribution runner error (worktree/pytest crash) → fail closed to
  today's behaviour, with the runner error in the dossier.

## Acceptance Criteria

- AC1: `tests/unit/test_wrapup_attribution.py::test_attribution_names_introducing_step`
  — fixture builds a THROWAWAY git repo in tmp_path with a base and
  two step commits, the second introducing a failure; the helper
  (with a real selector runner over the worktree) names step 2.
- AC2: `::test_attribution_reports_pre_existing_failure` — selector
  red at base → `pre_existing`, no step blamed.
- AC3: `::test_attribution_runner_error_fails_closed` — runner raising
  → closed result carrying the error, no exception escaping.
- AC4: `tests/unit/test_dev_runner_wrapup.py::test_wrapup_red_dispatches_attributed_repair`
  — stubbed loop receives exactly one brief carrying selector, step
  title, files and diff; `WRAPUP_REPAIR_ATTEMPTS` respected.
- AC5: `::test_wrapup_no_op_reason_names_step` — failed repair →
  session result's `no_op_reason` contains the step index/title and
  selector, and the dossier carries the attribution block.
- AC6: Integration:
  `tests/integration/test_wrapup_attribution_end_to_end.py::test_cross_cutting_breakage_attributed_and_repaired`
  — throwaway repo (tmp_path), two-step fake plan, step 2 breaks an
  unpromised test, a stub loop "repairs" it; the wrap-up ends green
  and the repair commit message names step 2. Hermetic: no network,
  no LLM, no reliance on a .env file.

## Open Questions

- OQ1: Should attribution also run for INTEGRATION-suite failures at
  wrap-up, or unit-only first? Default: unit-only first (integration
  reds are rarer and slower to walk).
