---
id: SP-DEV-PROMISE-DELIVERY
title: Promised tests delivered — strict reconciliation and mechanical rename
version: 0.1
status: approved
author: jfaye + Claude (six-occurrence failure pattern, 2026-07-04/05)
created: 2026-07-05
updated: 2026-07-05

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Promised tests delivered — strict reconciliation and mechanical rename

## Intent

Close the single most recurring operator-escalation pattern of the
autonomous Developer (six occurrences across four specs on
2026-07-04/05): promised test selectors delivered under drifted names
or not delivered at all, silently tolerated by the step gate's
file-level reconciliation, then fatally blocked by self-verify at
session end — every time costing one hand repair plus a full relaunch.

## Context

`execute_plan_step` (`src/ferova/review/dev_runner.py`) verifies a
step's promised `unit_tests` through `run_promised_tests`, which falls
back to a file-level run when the exact selectors fail to collect
(SP-DEV-PROMISE-RECONCILE). At the step gate the reconciled green is
accepted unconditionally (`dev_runner.py:935-941`), which conflates
two very different situations:

- The Developer wrote the tests this step under drifted names —
  reconciliation's intended case (intent proven, names wrong).
- The Developer never touched the promised test file at all — the
  pre-existing tests are green and the step passes with its promised
  tests entirely missing (observed on SP-BUDGET-RETRY-FIXES: both code
  fixes landed, zero promised tests written, step committed).

The gate can tell them apart: the loop's write set is already computed
at `dev_runner.py:844` (`_changed_paths` filtered by `pre_existing`)
before the gates run. Self-verify (`devagent_selfverify.py`) resolves
promised selectors literally via `selector_present`, so any drift that
survives the step gate becomes a terminal session failure later — the
worst place to discover it.

The six occurrences: SP-DEV-STEP-PREFLIGHT (guard test omitted),
SP-PLAN-CONTRACT-LINTS (docs-only test name drifted),
SP-USAGE-REASONING-SPLIT (integration test name drifted),
SP-AGENT-THINKING-CONTROL (loop test and integration file omitted),
SP-BUDGET-RETRY-FIXES (all three promised tests omitted),
SP-CHAINS-THINKING-CLASS (integration test name drifted).

## Goals

- G1: At the step gate, a reconciled green is accepted ONLY when the
  step's loop modified the promised test file in this attempt (the
  file appears in the step's changed-paths set). Otherwise the
  reconciliation is refused and the absence becomes a retryable gate
  like any other, with the exact missing selectors named in the
  feedback ("write tests named exactly: ...").
- G2: When a reconciled green IS accepted and the drift is
  unambiguous — exactly one promised node id missing from the file
  and exactly one test function present that no plan step promises —
  the runner mechanically renames the delivered function to the
  promised name, re-runs the exact selectors strictly, and proceeds
  only on green. Ambiguous drifts (several missing, several
  candidates) keep today's reconciled-accept and its warning log.
- G3: The preflight predicate and self-verify are unchanged — they
  already resolve selectors strictly; this spec makes the step gate
  stop shipping drift forward to them.

## Non-Goals

- NG1: No change to `run_promised_tests` itself or its use by the
  preflight predicate.
- NG2: No semantic matching of drifted names (no similarity
  heuristics): only the exactly-one-to-one case is renamed.
- NG3: No change to plan-form validation (the bare-file-promise lint
  is SP-PLAN-CONTRACT-LINTS-2, a separate slice).

## Assumptions

- A1: `src/ferova/review/dev_runner.py` is owned by
  SP-DEV-STEP-PREFLIGHT in the arch registry; this spec modifies it
  without claiming ownership (`owns.code: []` — ownership governs
  boundaries, not working sets) and adds no new intra-repo import, so
  the owner's declared edges stay sufficient.
- A2: The step's changed-paths set at gate time
  (`dev_runner.py:844`) faithfully reflects the loop's writes of this
  attempt (`pre_existing` filtering shipped in PR #12).

## Interface

Inputs: N/A (internal gate behaviour of `execute_plan_step`).

Outputs: N/A.

Errors: none raised — a failed mechanical rename (file unparseable,
re-run red) restores the file content and falls back to the retryable
gate of G1.

## Behavior

### Nominal

The Developer writes the promised test file with the promised names —
exact selectors green, nothing changes. With one drifted name in a
file the loop wrote, the runner renames it, the strict re-run is
green, the step commits carrying the promised selector.

### Edge cases

- Promised file untouched by the loop, pre-existing tests green →
  retryable gate feedback naming the missing selectors (G1), never a
  silent pass.
- Several promised node ids missing, or several unpromised test
  functions in the touched file → no rename, reconciled-accept with
  the existing warning (self-verify remains the backstop).
- The promised selector is a bare file path (no ``::``) → out of
  scope for G2 (nothing to rename); G1 applies unchanged.

### Failure scenarios

- Rename applied but the strict re-run stays red → file content
  restored, gate feedback as in G1, normal retry.

## Architecture Impact

- No edge added or removed; no ownership change. The modified file's
  owner (SP-DEV-STEP-PREFLIGHT) keeps its existing `depends_on`.

## Diagram

N/A (one gate branch + one bounded rewrite in a single module).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_plan_executor.py::test_untouched_promised_file_reconciliation_is_retried`
  — the fake Developer writes only source code while the promised
  test file pre-exists green; the gate feedback of the retry brief
  names the missing selectors, and a second attempt that writes them
  turns the step green (two dispatches).
- [ ] AC2: `tests/unit/test_review_plan_executor.py::test_touched_file_with_drifted_name_is_renamed_to_promise`
  — the fake writes the promised file with a single test under a
  drifted name; the step goes green in one attempt and the promised
  node id exists in the committed file.
- [ ] AC3: `tests/unit/test_review_plan_executor.py::test_ambiguous_drift_keeps_reconciled_accept`
  — two promised node ids missing and two unpromised test functions
  present in the touched file → no rename, step green via today's
  reconciled-accept.
- [ ] AC4: The full unit suite passes.

## Open Questions

(none)
