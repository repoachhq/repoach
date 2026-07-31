---
id: SP-DEV-STEP-SATISFIED-COMMIT
title: Step gate refuses a zero-value commit when promised tests were already green before the step ran
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Step gate refuses a zero-value commit when promised tests were already green before the step ran

## Intent

`execute_plan_step` can commit a plan step as successful even when the
step's own loop contributed nothing: its promised tests were already
passing on the tree BEFORE the step's Developer loop ran, and the
loop's only changes land inside the promised test file(s) themselves
(a cosmetic touch, a rewrite that changes nothing observable) —
because an EARLIER step (or earlier round of a multi-round-refined
plan) already delivered the real work. The runner has no baseline of
"were these tests already green before this step started" and so
credits the later, contribution-free step with the earlier step's
work. Add that baseline and refuse the commit when the step's diff
proves nothing beyond what already held true.

## Context

Finding evidence (2026-07-24, current `develop` HEAD):

- `src/repoach/review/dev_runner.py:1410-1428` — inside
  `execute_plan_step`'s attempt loop, `run_promised_tests` runs ONCE,
  AFTER the Developer's loop has already produced its diff. The
  `if reconciled:` block (checking `touched_promised` — did the loop's
  own changes touch the promised test file) only runs when the
  selectors needed the file-level fallback. When `reconciled` is
  `False` (the exact promised selectors already resolve and pass), NO
  check runs at all: the step is accepted regardless of whether the
  loop's diff had anything to do with that pass.
- `src/repoach/review/dev_runner.py:1470-1488` — `step_changes` (the
  paths this step is about to commit) is computed and checked only for
  contract escape (`escaped`), never for triviality, before
  `commit_paths` lands the commit.
- `src/repoach/review/dev_runner.py:401-490`
  (`step_preflight_complete`) already runs a *pre-dispatch* strict
  green check to skip re-running a step whose files all exist and
  whose tests already pass — but it requires every path in
  `step.files` to exist on disk first, and a `reconciled` (file-level
  fallback) pass is explicitly treated as "not proof" (line 415-420
  docstring) and disqualifies the skip. When any promised test file is
  still missing, or the pass is reconciled, preflight returns `False`
  and the step is DISPATCHED to the Developer loop — and it is that
  dispatched-and-run path (`execute_plan_step`, not the preflight
  skip) that has no equivalent baseline check.
- `src/repoach/review/dev_runner.py:174,1841-1903` — every
  `no_op_reason` is a session-level early return (spec not found,
  branch switch failed, push failed); none of them fire for a
  per-step, contribution-free commit.
- `ca279a6` (`fix(dev-runner): retire mechanical test-rename promise
  laundering`, 2026-07-24, this week) closed a narrower, related gap:
  a touched promised file whose promised selector name is absent now
  fails the gate instead of being laundered by a mechanical rename.
  That fix only fires on the `reconciled` branch and only checks
  selector-NAME presence — it does nothing for the exact-match branch,
  and nothing checks whether the step's OWN diff is what makes the
  tests pass versus tests that were already green beforehand.
- Live incidents referenced in the operator's judge dossier: this
  exact shape has recurred twice (SLOW-STRIKE step 5, REGEN step 6
  v1) — a step preflighted red (or wasn't checked), ran anyway, and
  was accepted as complete while its own commit carried no
  substantive change.

`dev_runner.py` is owned by an existing spec (SP-DEV-STEP-PREFLIGHT);
this is an in-place modification, consistent with how
SP-PROMISE-RENAME-RETIRE and SP-DEV-PROMISE-DELIVERY previously
touched the same file.

## Goals

- G1: Before `execute_plan_step` dispatches the FIRST attempt to the
  Developer loop for a step, capture a baseline: are the step's
  promised tests (`step.unit_tests`) ALREADY strictly green (every
  promised test file exists on disk, every node-id selector resolves,
  and `run_promised_tests` returns `(True, _, reconciled=False)`) on
  the tree as it stands right now? A `reconciled` pass is NOT proof
  (mirrors the existing `step_preflight_complete` precedent) and must
  NOT count as a green baseline.
- G2: After the step's post-loop gate reaches its existing
  `tests_ok` pass (`dev_runner.py:1410`), if the baseline from G1 was
  green AND every path in the step's own commit (`step_changes`,
  `dev_runner.py:1470`) is one of the step's promised test files (no
  path outside that set changed), refuse to commit the step as a
  normal success: return a failed, NOT-retried `StepOutcome` (mirrors
  the existing `step_promises_absent_tests` terminal pattern at
  `dev_runner.py:1397-1409`) naming the promised selectors and stating
  that the tests were already green before this step ran and its
  commit touches nothing beyond the promised test file(s) — the work
  was already delivered elsewhere and credit must be folded back into
  whichever earlier step actually made the tests pass. The step's
  uncommitted diff is left in place for inspection (no revert).
- G3: A step whose commit touches ANY file outside its promised test
  files (a real source change, a genuinely new test file, a docs
  file, etc.) is UNAFFECTED by this gate regardless of the baseline —
  G2 only fires on the narrow "diff confined entirely to already-green
  promised test file(s)" shape.
- G4: A step whose promised test file(s) do not yet exist, or whose
  baseline run is red, or whose baseline pass is `reconciled`
  (fallback, not proof), is UNAFFECTED — the baseline is `False` and
  G2 never fires. This is the mechanism that keeps ordinary
  "write the test, make it pass" steps working exactly as before.

## Non-Goals

- NG1: no automatic plan-step credit reattribution or plan rewriting.
  This spec detects and REFUSES the zero-value commit; it does not
  rewrite an earlier step's commit or the plan document to fold the
  credit in mechanically — that stays a human/operator replanning
  action, same as the existing `step_promises_absent_tests` terminal
  path already requires for a mis-shaped plan.
- NG2: no change to `step_preflight_complete`'s own pre-dispatch skip
  behavior (`dev_runner.py:401-490`) — it keeps skipping a step
  outright when every file exists and the strict pass is proven; this
  spec only adds the missing symmetric check to the path that RUNS
  when preflight declines to skip.
- NG3: no change to `ca279a6`'s promise-delivery / rename-laundering
  checks (the `touched_promised` / `absent_promises` block at
  `dev_runner.py:1414-1462`) — this spec adds an INDEPENDENT check
  that also covers the exact-match (`reconciled=False`) branch that
  block does not run on.
- NG4: a step whose diff is confined to its promised test file(s) but
  whose baseline was genuinely RED (the file existed with a failing
  or absent assertion) is a legitimate "make the test pass" step and
  is NOT flagged — only an ALREADY-green baseline triggers G2.
- NG5: no behavior change to the session-level `no_op_reason` early
  returns (`dev_runner.py:1841-1903`) or to any gate upstream of the
  attempt loop (syntax, imports, ruff, repo lint).

## Assumptions

- A1: `run_promised_tests`'s existing `(ok, tail, reconciled)` return
  shape is sufficient to distinguish a proven strict pass from an
  unproven file-level fallback pass — the same distinction
  `step_preflight_complete` already relies on.
- A2: The baseline call runs on the tree exactly as it stands when
  `execute_plan_step` is entered (before any attempt), so it reflects
  "what was true before this step touched anything," not "what is
  true after some earlier attempt of THIS SAME step already edited
  files" — the baseline is computed ONCE per step invocation, not
  once per attempt.

## Interface

`src/repoach/review/dev_runner.py`:

```python
def _step_tests_already_green(repo_root: Path, step: PlanStep) -> bool:
    """Return True when every promised test in *step* strictly passes right now.

    Mirrors the strict-pass rule `step_preflight_complete` already
    applies: every promised test file must exist, every node-id
    selector must resolve, and `run_promised_tests` must return
    `reconciled=False`. Wrapped in a broad try/except (subprocess and
    filesystem errors) that logs `dev_runner.step_baseline_error` and
    returns `False` (fail-open — an unreadable baseline never blocks
    a step, it only forfeits the zero-value check for that step).
    """
```

- `execute_plan_step` calls `_step_tests_already_green(repo_root,
  step)` exactly once, before the `for attempt in
  range(1, _MAX_STEP_ATTEMPTS + 1)` loop, and stores the result as
  `baseline_green: bool`.
- Immediately after the existing `tests_ok` check at
  `dev_runner.py:1410-1413` reaches a green result (whether or not
  `reconciled`), and before the `step_changes` / `escaped` / commit
  block (`dev_runner.py:1470-1488`), a new check:

```python
if baseline_green:
    promised_files = {s.split("::", 1)[0] for s in step.unit_tests}
    non_test_changes = [p for p in step_changes if p not in promised_files]
    if not non_test_changes:
        totals.reason = ...
        _log.warning("dev_runner.step_zero_value_diff", ...)
        return totals
```

  placed after `step_changes` is computed (`dev_runner.py:1470`) and
  before `commit_paths` is called (`dev_runner.py:1488`), so the
  refusal sees the exact path set that would otherwise be committed.
- No public signature changes: `execute_plan_step`'s parameters and
  `StepOutcome` fields are unchanged; the new terminal path sets
  `totals.reason` and returns `totals` with `totals.ok` left at its
  default `False`, identically to the existing
  `step_promises_absent_tests` terminal return.

## Behavior

### Nominal

- A step's promised test file does not exist yet (or exists but is
  red) → baseline is `False` → the step runs and commits exactly as
  today when it goes green.
- A step's promised tests are already green at baseline AND the
  loop's diff touches a real source file (or any file outside the
  promised test set) → `non_test_changes` is non-empty → the step
  commits normally; G2 never fires.

### Edge cases

- Baseline pass is `reconciled=True` (file-level fallback) →
  `baseline_green` is `False` per G1/G4 → G2 never fires, even if the
  step's eventual diff is test-file-only.
- A step promising a BARE file selector (no `::`) whose file already
  contains unrelated passing tests → `_step_tests_already_green`
  requires every promised selector to resolve statically
  (`selector_present`) before trusting a pass; a bare-file selector
  with no node id is treated the same way `step_preflight_complete`
  already treats it (no per-symbol proof beyond the file existing and
  the run being green and non-reconciled) — this spec does not change
  that existing ambiguity, only reuses it identically for the
  baseline.
- A step whose contract lists BOTH a source file and its test file,
  and the loop only edits the test file cosmetically while the source
  file is untouched, with baseline already green → flagged by G2
  (this is the exact shape the finding describes).
- The baseline subprocess call raises (timeout, unreadable file) →
  caught, logged as `dev_runner.step_baseline_error`, `baseline_green`
  defaults to `False` (fail-open; the step is never blocked by a
  baseline-measurement failure).

### Failure scenarios

- G2 fires → `execute_plan_step` returns immediately with
  `totals.ok=False` and a `totals.reason` naming the promised
  selectors, the touched-but-insufficient path set, and stating the
  work must be folded back into whichever earlier step already
  delivered it. The step is NOT retried (mirrors
  `step_promises_absent_tests`); the loop's uncommitted diff is left
  on disk for inspection, never reverted.

## Architecture Impact

- Adds/Removes dependency: none — in-place addition inside
  `execute_plan_step` and one new private helper in the same module
  (`dev_runner.py`, owned by SP-DEV-STEP-PREFLIGHT); no new
  cross-owner import, no new `owns.code` edge.
- New / changed coupling, cycles, or shared state: none — the new
  helper calls the module's own existing `run_promised_tests` and
  `selector_present`; no new shared state.

## Diagram

N/A (single-function control-flow addition inside an existing loop).

## Acceptance Criteria

- [ ] AC1: unit — new file `tests/unit/test_dev_runner_step_zero_value.py`,
  driving `execute_plan_step` with a truthful boundary-fake Developer
  (no monkeypatching of repoach functions) exactly like
  `test_dev_runner_promise_delivery.py`'s rig:
  `test_zero_value_step_refused_when_tests_already_green_before_step_ran`
  — a tmp repo whose initial commit already contains a promised test
  file with a passing `def test_thing(...)` (baseline strictly green,
  non-reconciled); the step's `files` list BOTH that test file and an
  untouched source file. The fake Developer loop, on every attempt,
  rewrites ONLY the test file with a cosmetic change (an extra
  trailing blank line) and never touches the source file. Asserts
  `outcome.ok is False`, that the on-disk source file is byte-identical
  to its initial content (never committed as changed), and that
  `outcome.reason` names the step's promised selector
  (`tests/unit/test_thing.py::test_thing`). This test MUST FAIL on
  pre-change code (today `outcome.ok is True` and the cosmetic-only
  diff is committed).
- [ ] AC2: unit — same file,
  `test_step_commits_normally_when_baseline_green_but_source_file_also_changed`
  — identical fixture, except the fake Developer loop ALSO writes a
  real change into the source file every attempt. Asserts
  `outcome.ok is True` and the source file's new content is committed
  — proves G3 (a step touching a non-test file is never flagged, even
  with a green baseline).
- [ ] AC3: unit — same file,
  `test_step_with_red_baseline_and_test_only_diff_still_commits` — the
  promised test file exists at init but its assertion FAILS (baseline
  red); the fake Developer loop fixes the assertion inside the test
  file only (still no source file touched). Asserts `outcome.ok is
  True` — proves G4/NG4 (a genuinely red-to-green test-only fix is
  never confused with a zero-value step).
- [ ] AC4: regression — the full existing
  `tests/unit/test_dev_runner_promise_delivery.py`,
  `tests/unit/test_dev_promise_reconcile.py`, and
  `tests/unit/test_dev_runner_promise_helpers.py` suites stay green
  unmodified (proves the new baseline check does not alter the
  `reconciled` branch's existing behavior).
- [ ] AC5: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  net new non-test code ≤ 120 lines.

## Open Questions

None.
