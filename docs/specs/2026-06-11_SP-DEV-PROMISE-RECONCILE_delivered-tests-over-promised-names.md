# SP-DEV-PROMISE-RECONCILE — the pytest gate accepts delivered tests over promised names

## Metadata

- **Status**: OPEN
- **Priority**: P1 (unblocks the reliability of every future dispatch)
- **Owner**: operator
- **Executor**: `ferova develop` (with the noted irony: this
  dispatch can itself trip on the trap it fixes — hand-ship on stall)
- **Opened**: 2026-06-11

## Why

Recurrent dispatch killer, observed again live on the SP-REVIEW-MEMORY
run (2026-06-11): the Planner *invents* exact test ids in
`step.unit_tests`, then the Developer must reproduce them character
for character in the test file it writes. LLMs paraphrase — the
SP-REVIEW-MEMORY Developer wrote a correct module plus a 2.7 KB test
file, but under different function names. `execute_plan_step`'s
pytest gate ran the *promised* selectors, pytest reported
`found no collectors`, and the step was reverted — twice, because the
gate feedback ("create exactly these...") is already fed back into the
retry brief and still did not produce verbatim reproduction. The
builder seed lesson covering this is recalled before planning and did
not help either. It is a harness defect, not a prompting defect: the
gate verifies *promised names* when its actual intent is *delivered,
passing tests*.

Same root flaw as the review verdict, same medicine as the redesign:
**the plan is a hint; the delivered tree is the truth.**

## What

In `src/ferova/review/dev_runner.py`:

1. New helper `run_promised_tests(repo_root: Path, selectors:
   list[str]) -> tuple[bool, str, bool]` returning
   `(green, output_tail, reconciled)`:
   - Run `run_pytest_selectors` on the exact promised selectors —
     green → `(True, tail, False)` (exact match stays the happy path).
   - On failure, fall back to the promised **files**
     (`sorted({s.split("::", 1)[0] for s in selectors})`) and run
     those. Green → `(True, tail, True)`. pytest exits non-zero on a
     file collecting zero tests, so an empty delivered test file can
     never reconcile.
   - Both red → `(False, file_level_tail, False)` (the file-level
     output is the more informative feedback for the retry brief).
2. `execute_plan_step` calls the helper where it currently calls
   `run_pytest_selectors` directly; on `reconciled=True` emit
   `dev_runner.promised_tests_reconciled` (warning level, with
   `spec_id`, `step`, `promised` ids) so reconciliations stay visible
   in every run log, then proceed to commit exactly as a green exact
   match would.
3. The absent-file check (`step_promises_absent_tests`, revert without
   retry) stays untouched — a step whose promised test *files* are
   missing is still a mis-shaped plan.
4. Update the module docstring's gate description.

Safety net unchanged: `run_pytest_matrix` still runs the full suite
after all steps — a reconciled step that broke something distant is
still caught before push.

## Files in scope

- `src/ferova/review/dev_runner.py`
- `tests/unit/test_dev_promise_reconcile.py` (new)

## Plan-shaping constraint (post-mortem of the first dispatch)

The first dispatch died twice on step 2 with
`SyntaxError: unterminated string literal` — output truncation while
full-file-rewriting `dev_runner.py` (821 lines) **plus**
`test_review_dev_runner.py` (397 lines) in one step. The plan MUST:

- put every new test in the NEW file
  `tests/unit/test_dev_promise_reconcile.py` — never contract the
  large existing `test_review_dev_runner.py` for modification;
- never contract `dev_runner.py` together with any other large file
  in the same step (one big file per step, maximum).

## Out of scope

- Inverting name ownership (plan promises file + minimum count, the
  Developer declares created ids) — deeper contract change, revisit
  after the redesign's finding model lands.
- The end-of-run integration-tests gate (already file-level).
- Any plan-model (`plan.py`) change.

## Smoke scenario

### Setup

A tmp git repo with one committed file and a plan step promising
`tests/unit/test_x.py::test_promised_name`.

### Execute

Run `execute_plan_step` with a fake Developer that writes
`tests/unit/test_x.py` containing a single *passing*
`def test_delivered_name()` (mismatched on purpose), then again with a
fake writing a *failing* test, then with one writing an *empty* file.

### Expected

Mismatched-but-green: step commits, `promised_tests_reconciled`
logged. Failing: revert + retry as today. Empty file: red (zero
collected), revert — never reconciled.

## Definition of Done

- Exact promised ids green → accepted with `reconciled=False` and no
  reconciliation log — `test_exact_promised_ids_stay_happy_path`.
- Promised ids absent but promised files contain passing tests →
  accepted, `reconciled=True`, warning emitted —
  `test_mismatched_names_reconcile_to_delivered_tests`.
- Promised files red or empty → `(False, tail, False)` —
  parametrised `test_red_or_empty_delivered_tests_stay_red`.
- `execute_plan_step` commits a reconciled step (end-to-end with fake
  Developer on a tmp repo) — `test_step_commits_on_reconciled_tests`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(dev): pytest gate reconciles promised test ids to delivered tests`
2. `test(dev): promise-reconcile happy path, mismatch, red and empty cases`

## Risks

- **Plan/delivery drift becomes invisible**: mitigated by the
  warning-level reconciliation log — drift is observable, just no
  longer fatal.
- **A reconciled file passing for the wrong reason** (e.g. the
  Developer delivered unrelated trivial tests): the full-suite matrix
  gate plus the review bench still stand between the branch and
  develop.
