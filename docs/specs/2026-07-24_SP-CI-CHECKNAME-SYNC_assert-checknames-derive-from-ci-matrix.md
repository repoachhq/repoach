---
id: SP-CI-CHECKNAME-SYNC
title: Assert DEFAULT_REQUIRED_CHECK_NAMES derives from ci.yml's matrix, not a hand-copy
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - tests/unit/test_ci_checkname_sync.py
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Assert DEFAULT_REQUIRED_CHECK_NAMES derives from ci.yml's matrix, not a hand-copy

## Intent

`src/repoach/review/auto_merge.py`'s `DEFAULT_REQUIRED_CHECK_NAMES` is a
hardcoded tuple that mirrors `.github/workflows/ci.yml`'s
`matrix.python-version` list and the job's `name: Test suite (Python
${{ matrix.python-version }})` template purely by hand-maintained
convention — the module's own docstring admits it ("mirrors the matrix
in `.github/workflows/ci.yml`. Override per call when the matrix
changes."). Nothing enforces the sync: a future matrix edit (adding
3.14, dropping 3.11) that forgets to touch `auto_merge.py` desynchronizes
the auto-merge CI gate from the real required-check names, and the gate
fails closed on a phantom missing check or, worse, could pass evaluating
a stale name that no longer corresponds to any real job. Add a cheap
unit test that parses `ci.yml`'s matrix and independently derives the
same set of check-name strings, then asserts equality against
`DEFAULT_REQUIRED_CHECK_NAMES`, so a matrix edit that forgets the
constant fails CI immediately instead of silently drifting.

## Context

Re-verified against `develop` at HEAD (`origin/develop`, re-grepped
2026-07-31 — the finding is unchanged since it was first logged in
`docs/tech_debt.md` item 12):

- `src/repoach/review/auto_merge.py:120-123`:

  ```python
  DEFAULT_REQUIRED_CHECK_NAMES: tuple[str, ...] = (
      "Test suite (Python 3.11)",
      "Test suite (Python 3.13)",
  )
  ```

  A hardcoded tuple, currently still in sync with the matrix but with
  no mechanism enforcing that.

- `src/repoach/review/auto_merge.py:33-35` (module docstring):
  `:data:`DEFAULT_REQUIRED_CHECK_NAMES` mirrors the matrix in
  ``.github/workflows/ci.yml``.  Override per call when the matrix
  changes.` — an explicit admission that the sync is a hand-maintained
  convention, not a derived invariant.

- `.github/workflows/ci.yml:28`: `name: Test suite (Python ${{
  matrix.python-version }})` — the job-name template that, combined
  with the matrix values, produces the exact strings GitHub reports in
  `statusCheckRollup`.

- `.github/workflows/ci.yml:40`: `python-version: ["3.11", "3.13"]` —
  the matrix `DEFAULT_REQUIRED_CHECK_NAMES` must track.

- `DEFAULT_REQUIRED_CHECK_NAMES` is consumed as the default
  `required_names: Sequence[str]` parameter across five functions in
  `auto_merge.py` (`evaluate_ci_gate`, `run_auto_merge`,
  `evaluate_merge_gate`, and two more — confirmed via `grep -n
  DEFAULT_REQUIRED_CHECK_NAMES src/repoach/review/auto_merge.py`,
  6 matches: the definition plus 5 call sites), so a silent
  desynchronization affects every merge-gate evaluation path, not one
  isolated call.

- No existing test in the repo cross-checks this pair: `grep -rl
  "DEFAULT_REQUIRED_CHECK_NAMES" tests/` finds only tests that *use*
  the constant as a fixture value (`test_automerge_fail_fast_gate.py`,
  `test_review_gate.py`, `test_automerge_fresh_head.py`,
  `test_review_auto_merge.py`); none of them parse `ci.yml` or assert
  the derivation. `docs/tech_debt.md` item 12 documents exactly this
  gap and remains open.

- `src/repoach/review/auto_merge.py` is not listed under any spec's
  `owns.code` (`grep -rl "auto_merge.py" docs/specs/*.md | xargs grep
  -l "owns:"` turns up six specs that mention or edit the file in
  prose, but every one of them declares `owns: code: []` in its
  frontmatter) — it is presently unowned. This spec adds a new,
  standalone test file and reads (never writes) both `ci.yml` and
  `auto_merge.py`; it introduces no edit to either, so no ownership
  claim over `auto_merge.py` or `.github/workflows/ci.yml` is needed.

- `pyyaml>=6.0` is already a project dependency (`pyproject.toml:20`)
  and already used to parse workflow/config YAML in the test suite
  (`src/repoach/arch/registry.py`, `tests/unit/test_spec_template.py`),
  so the new test needs no new dependency.

## Goals

- G1: a new unit test loads `.github/workflows/ci.yml`, reads the
  `jobs.test.strategy.matrix.python-version` list and the
  `jobs.test.name` template, and derives the set of expected
  check-name strings by substituting each matrix entry into the
  template — the same substitution GitHub Actions performs to produce
  `statusCheckRollup` check names.
- G2: the test asserts that derived set equals
  `set(DEFAULT_REQUIRED_CHECK_NAMES)` imported from
  `repoach.review.auto_merge`, failing loudly (a plain assertion
  diff naming both sets) when they diverge.
- G3: the test is skip-free and mock-free — it reads the real
  `.github/workflows/ci.yml` file from the repo root (resolved via
  `Path(__file__).resolve().parents[2]`, matching the existing
  `tests/unit/` → repo-root convention) and the real
  `DEFAULT_REQUIRED_CHECK_NAMES`, so a matrix edit that forgets the
  constant is caught the moment `pytest tests/unit` runs, with zero
  extra CI wiring.
- G4: the equality check is demonstrably discriminating, not vacuously
  true — proved by AC1's live pass against today's still-synced pair
  and by AC2's synthetic desync scenario, which asserts the same
  comparison mechanism correctly reports inequality when the matrix
  and the constant diverge.

## Non-Goals

- NG1: no behavior change beyond adding one new test file —
  `DEFAULT_REQUIRED_CHECK_NAMES`, `.github/workflows/ci.yml`, and
  every function in `auto_merge.py` are untouched; this spec adds a
  regression guard, it does not fix or refactor the mirrored pair.
- NG2: no generalization to a job-name-template parser reusable
  beyond this one job — the test is scoped to the single `test` job
  in `ci.yml` that `DEFAULT_REQUIRED_CHECK_NAMES` mirrors; other
  workflows (`auto-review.yml`) are out of scope.
- NG3: no change to how `evaluate_ci_gate` or any auto-merge function
  consumes `required_names` at runtime — this is a static, offline
  consistency check between two committed sources of truth, not a
  runtime derivation that replaces the constant.
- NG4: no new lint script or pre-commit hook — this is a plain
  `pytest` unit test living in `tests/unit/`, not a new
  `scripts/lint_*.py` gate.

## Interface

New file only, no changes to existing signatures:

`tests/unit/test_ci_checkname_sync.py`:

```python
def test_default_required_check_names_matches_ci_matrix() -> None: ...
```

Reads `.github/workflows/ci.yml` via `yaml.safe_load`, extracts
`workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"]`
and `workflow["jobs"]["test"]["name"]`, builds the expected set by
formatting the name template with each matrix version substituted for
the `${{ matrix.python-version }}` placeholder, and compares it against
`set(repoach.review.auto_merge.DEFAULT_REQUIRED_CHECK_NAMES)`.

## Behavior

### Nominal

- `ci.yml`'s matrix is `["3.11", "3.13"]` and its job name template is
  `Test suite (Python ${{ matrix.python-version }})` (today's state) →
  the derived set is `{"Test suite (Python 3.11)", "Test suite (Python
  3.13)"}`, which equals `set(DEFAULT_REQUIRED_CHECK_NAMES)` → the test
  passes.

### Edge cases

- The matrix grows (e.g. `["3.11", "3.12", "3.13"]`) but
  `DEFAULT_REQUIRED_CHECK_NAMES` is not updated → the derived set has
  three entries, the constant's set has two → the assertion fails with
  a diff naming `Test suite (Python 3.12)` as present-in-derived,
  missing-in-constant.
- The matrix shrinks (a version is dropped) but the constant keeps the
  stale entry → symmetric failure, the stale name shows as
  present-in-constant, missing-in-derived.
- The job name template itself changes (e.g. drops `(Python
  ${{ matrix.python-version }})`) → the derived strings no longer match
  any entry in the constant → the test fails, flagging the template
  change as unaccounted for even though the version list itself did
  not move.

### Failure scenarios

- `.github/workflows/ci.yml` is missing, unparseable, or restructures
  `jobs.test.strategy.matrix.python-version` / `jobs.test.name` under
  different keys → the test raises a `KeyError`/`yaml.YAMLError` at
  collection time, which pytest reports as a test error — a loud
  signal that the workflow's shape moved out from under the sync
  check, rather than a silent pass on an empty derived set.

## Architecture Impact

- Adds/Removes dependency: none — no new module, no new import edge
  between `tests/` and `src/repoach/review/auto_merge.py` beyond the
  existing test-suite convention of importing the module under test;
  `pyyaml` is an existing dependency.
- New / changed coupling, cycles, or shared state: none.
  `.github/workflows/ci.yml` and `auto_merge.py` remain unowned by any
  spec; this spec's `owns.code` is exactly the one new test file it
  introduces, and it does not edit either mirrored source.

## Diagram

N/A (single new test file, no runtime code path changes).

## Acceptance Criteria

- [ ] AC1: unit —
  `tests/unit/test_ci_checkname_sync.py::test_default_required_check_names_matches_ci_matrix`.
  Load the real `.github/workflows/ci.yml`, derive the expected
  check-name set as described in Interface, and assert it equals
  `set(DEFAULT_REQUIRED_CHECK_NAMES)` imported from
  `repoach.review.auto_merge`. Must FAIL on pre-change code because
  the file does not yet exist (`ModuleNotFoundError` /
  `pytest --collect-only` finds nothing at that path); once added, it
  PASSES against today's still-synced pair.
- [ ] AC2: unit —
  `tests/unit/test_ci_checkname_sync.py::test_matrix_desync_is_detected`.
  Build a synthetic derived set from a locally constructed matrix list
  `["3.11", "3.12", "3.13"]` (simulating a future matrix bump) formatted
  through the same job-name template used in AC1, and assert it is NOT
  equal to `set(DEFAULT_REQUIRED_CHECK_NAMES)` (today's two-entry
  tuple) — proving the equality check AC1 relies on is discriminating
  (sensitive to real content, not vacuously true) and would catch a
  real desync the moment a matrix bump lands without a matching
  constant update. Must FAIL on pre-change code because the file does
  not yet exist.
- [ ] AC3: `ruff check` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE) and no `# noqa` anywhere in the
  diff; full `pytest tests/unit` green (including the two new tests
  above); `repoach arch graph --check` exits 0 (no new ownership
  conflict — the sole new file, `tests/unit/test_ci_checkname_sync.py`,
  is claimed under this spec's own `owns.code` and owned by no other
  spec).

## Open Questions

None.
