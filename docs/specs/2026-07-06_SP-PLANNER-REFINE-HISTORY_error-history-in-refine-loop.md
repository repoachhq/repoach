---
id: SP-PLANNER-REFINE-HISTORY
title: Error history in the Planner's refine loop
version: 0.1
status: approved
author: jfaye + Claude (nine whack-a-mole attempts across three planning sessions, 2026-07-06)
created: 2026-07-06
updated: 2026-07-06

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Error history in the Planner's refine loop

## Intent

Stop the refine loop's whack-a-mole. Three planning sessions
(SP-PLANNER-SELECTOR-CHECK twice, SP-CLAIM-TYPE-ROUTING once) burned
their full retry budgets — nine attempts, nine DIFFERENT validation
errors — because each refine turn carries only the LAST error: the
model fixes it and reintroduces one it fixed two turns earlier, or
trips a rule it was never shown.

## Context

`_refine_prompt` (`src/ferova/review/planner.py:60-76`) embeds the
rejected candidate (6 000-char cap) and the single latest error;
`_PLAN_PARSE_ATTEMPTS = 3` (`planner.py:38`) is a module constant.
The retry loops (`_plan_via_proxy` `:325-354`, `_plan_via_cc`
`:372-413`) already accumulate nothing. The economics note in the
docstring ("re-running the full tool loop would re-pay the whole
exploration") stays true — refinement turns remain tool-less
one-shots; they just need the full picture.

Suggested plan shape (this spec's own planning must not whack-a-mole):

- Step 1 — files `[src/ferova/review/planner.py,
  tests/unit/test_review_planner.py]` (the test file EXISTS and must
  sit in this step's files), promising exactly the four AC node ids,
  all NEW module-level tests appended to that file.
- Step 2 — files `[tests/integration/test_planner_refine_history.py]`
  (NEW file), promising
  `tests/integration/test_planner_refine_history.py::test_two_error_session_converges_with_history`
  in both its `unit_tests` and the plan-level `integration_tests`.

## Goals

- G1: The refine prompt carries the session's FULL error history —
  every prior attempt's error, numbered, oldest first, each truncated
  to 300 chars — under an explicit instruction: "your next candidate
  must satisfy ALL of these at once; re-check each before answering."
- G2: `_PLAN_PARSE_ATTEMPTS` becomes a setting
  (`FEROVA_PLANNER_PARSE_ATTEMPTS`, default 5) read once per session;
  the constant's semantics (1 initial + N-1 refinements) are
  unchanged.
- G3: Every rejection log line (`planner.plan_invalid`) gains an
  `errors_so_far` count so a whack-a-mole session is visible in logs.

## Non-Goals

- NG1: No re-exploration on refine (the economics stand).
- NG2: No change to `_parse_and_validate` or the selector check.
- NG3: No cross-session memory of errors.

## Assumptions

- A1: `planner.py` is frontier; this spec owns nothing.
- A2: The history section stays small (5 × 300 chars max) relative to
  the 6 000-char candidate embed.

## Interface

Inputs:
- `FEROVA_PLANNER_PARSE_ATTEMPTS` (env, default 5).
- `_refine_prompt(candidate, errors: list[str])` — signature change
  from a single error to the ordered history.

Outputs: N/A.

Errors: none new.

## Behavior

### Nominal

Attempt 1 fails on a bare-file promise; attempt 2's prompt lists that
error; attempt 2 fails on coupling; attempt 3's prompt lists BOTH,
and the model checks both before answering.

### Edge cases

- First refinement (one error) → history of one, same behaviour as
  today.
- Setting below 1 → clamped to 1 (initial attempt only, no refine).

### Failure scenarios

- All attempts exhausted → loud failure with the full history in the
  session error (today: only the last).

## Architecture Impact

- No edge added or removed; no ownership change.

## Diagram

N/A (prompt assembly + one setting).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_review_planner.py::test_refine_prompt_carries_full_error_history`
  — after two rejections the third prompt contains both errors,
  numbered, oldest first.
- [ ] AC2: `tests/unit/test_review_planner.py::test_parse_attempts_setting_is_honored`
  — with the env set to 2, a never-valid candidate makes exactly 2
  attempts.
- [ ] AC3: `tests/unit/test_review_planner.py::test_exhausted_session_reports_full_history`
  — the final error names every attempt's failure.
- [ ] AC4: `tests/unit/test_review_planner.py::test_single_error_history_matches_previous_behaviour`
  — one rejection produces a prompt equivalent to today's shape.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
