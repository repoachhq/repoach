---
id: SP-TEST-BASENAME-GATE
title: Package the test tree and gate duplicate unit/integration test basenames
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code:
    - tests/__init__.py
    - tests/unit/__init__.py
    - tests/integration/__init__.py
    - src/repoach/lint/no_duplicate_test_basenames.py
    - scripts/lint_no_duplicate_test_basenames.py
    - .githooks/pre-commit
    - tests/unit/test_test_tree_packaging.py
    - tests/unit/test_no_duplicate_test_basenames_gate.py
  resources: N/A

depends_on: []
provides_to: []

constraints: {}
---

# Package the test tree and gate duplicate unit/integration test basenames

## Intent

`tests/`, `tests/unit/` and `tests/integration/` have no `__init__.py`,
so pytest's default (`prepend`) import mode identifies each collected
test file by its bare module name. Six file basenames currently exist
in both `tests/unit/` and `tests/integration/` at once; collecting any
one of those pairs together — exactly what `testpaths = ["tests"]` in
`pyproject.toml` declares as the project's own default — raises an
opaque `import file mismatch` `ERROR` that aborts collection of the
*entire* run, unrelated to whatever change triggered the debugging
session. Package the three test directories so pytest resolves fully
qualified, non-colliding module names (`tests.unit.test_x` vs
`tests.integration.test_x`), and add a ratcheting lint gate — mirroring
the existing `no_inline_comments` / `no_silent_except` gates — so a
future PR cannot silently reintroduce a new collision.

## Context

- `pyproject.toml:111`: `testpaths = ["tests"]` — the project's own
  declared default is to collect `tests/unit` and `tests/integration`
  together, which is exactly the configuration that triggers the bug.
- No `__init__.py` exists in `tests/`, `tests/unit/`, or
  `tests/integration/` (verified: `find tests -maxdepth 1
  -name "__init__.py"` returns nothing in any of the three
  directories).
- Reproduced live at HEAD:
  `python -m pytest --collect-only tests/unit/test_review_dev_runner.py
  tests/integration/test_review_dev_runner.py` raises:
  ```
  import file mismatch:
  imported module 'test_review_dev_runner' has this __file__ attribute:
    tests/unit/test_review_dev_runner.py
  which is not the same as the test file we want to collect:
    tests/integration/test_review_dev_runner.py
  HINT: remove __pycache__ / .pyc files and/or use a unique basename
  for your test file modules
  ```
- `comm -12 <(ls tests/unit) <(ls tests/integration)` currently shows 6
  colliding test-file basenames: `test_agent_thinking_control.py`,
  `test_automerge_fail_fast_gate.py`, `test_chains_audit_cli.py`,
  `test_findings_bridge.py`, `test_review_dev_runner.py`,
  `test_review_merge_exit_contract.py`.
- `.github/workflows/ci.yml` currently runs `pytest -q tests/unit
  -n auto --dist worksteal` and `pytest -q tests/integration -n auto
  --dist worksteal` as two *separate* steps, which is the only reason
  the collision has not already broken CI — it is caught only by luck
  whenever a human or agent happens to run both suites together (as
  already happened once this week, fixed after the fact by renaming).
- `src/repoach/lint/no_inline_comments.py` and
  `src/repoach/lint/no_silent_except.py` establish the house pattern
  for a ratcheting scanner gate (module in `src/repoach/lint/`, CLI
  wrapper in `scripts/`, pytest binding with a `MAX_*` baseline
  constant that can only decrease) that this spec's new gate follows.
- `.github/workflows/ci.yml` and `prompts/review/*` are bot-forbidden
  paths; this spec does not touch either. Wiring the new lint into CI
  is recorded as an operator-manual follow-up (AC6).

## Goals

- G1: `tests/`, `tests/unit/`, and `tests/integration/` are Python
  packages (empty `__init__.py` in each), so pytest's `prepend` import
  mode resolves collected test modules under fully qualified dotted
  names (`tests.unit.test_x`, `tests.integration.test_x`) instead of
  bare basenames.
- G2: collecting a colliding pair together (e.g.
  `tests/unit/test_review_dev_runner.py` and
  `tests/integration/test_review_dev_runner.py` in one invocation, or
  the whole `tests/` tree per `testpaths`) no longer raises `import
  file mismatch`.
- G3: a new ratcheting lint (`repoach.lint.no_duplicate_test_basenames`)
  scans `tests/unit/` and `tests/integration/` for basenames present in
  both, exposed via `scripts/lint_no_duplicate_test_basenames.py`
  (mirroring the existing two lint CLIs) and wired into
  `.githooks/pre-commit`, so a future PR that reintroduces a new
  colliding basename fails the local gate even though G1 already makes
  it collectible.
- G4: the gate starts at the current baseline (6 colliding basenames)
  and can only ratchet down, exactly like `MAX_VIOLATIONS` in
  `test_no_inline_comments_gate.py` — it does not force an immediate
  rename of the 6 existing pairs.

## Non-Goals

- NG1: no behavior change beyond G1-G4 — no test logic inside any
  existing `tests/unit/*.py` or `tests/integration/*.py` file is
  modified, and none of the 6 currently-colliding files are renamed by
  this spec.
- NG2: no change to `.github/workflows/ci.yml` — bot-forbidden path.
  Wiring `scripts/lint_no_duplicate_test_basenames.py --summary` into
  the CI lint steps (alongside the existing two) is an operator-manual
  follow-up (AC6).
- NG3: no switch to `--import-mode=importlib` in `pyproject.toml` — the
  `__init__.py` packaging approach is chosen because it requires no
  `addopts` change and matches the two prior alternatives evaluated in
  the finding; `pyproject.toml`'s `[tool.pytest.ini_options]` block is
  untouched.
- NG4: no change to `no_inline_comments.py` or `no_silent_except.py` —
  the new gate is a sibling module, not a modification of the existing
  two.

## Interface

`src/repoach/lint/no_duplicate_test_basenames.py`:

```python
DEFAULT_UNIT_ROOT: str = "tests/unit"
DEFAULT_INTEGRATION_ROOT: str = "tests/integration"

def scan(unit_root: Path, integration_root: Path) -> list[str]:
    """Return the sorted basenames of ``test_*.py`` files present in
    both *unit_root* and *integration_root*."""

def summarise(duplicates: list[str]) -> dict[str, int]:
    """Return ``{"total": len(duplicates)}`` for CLI/pytest reporting."""
```

`scripts/lint_no_duplicate_test_basenames.py`:
- CLI wrapper mirroring `scripts/lint_no_silent_except.py`: `--summary`,
  `--unit-root`, `--integration-root`, `--max` flags; exits non-zero
  when the duplicate count exceeds `--max`.

`.githooks/pre-commit`:
- One additional staged block, following the existing
  `no-inline-comments` / `no-silent-except` blocks, invoking `python
  scripts/lint_no_duplicate_test_basenames.py --summary`.

## Behavior

### Nominal

- After `__init__.py` is added to all three directories,
  `python -m pytest --collect-only tests/unit/test_review_dev_runner.py
  tests/integration/test_review_dev_runner.py` collects both files
  with zero errors — pytest resolves them as
  `tests.unit.test_review_dev_runner` and
  `tests.integration.test_review_dev_runner`.
- `python -m pytest --collect-only tests/` (the bare-tree invocation
  implied by `testpaths = ["tests"]`) collects the full tree with zero
  `import file mismatch` errors.
- `python scripts/lint_no_duplicate_test_basenames.py --summary` on the
  unmodified repo reports `total=6` and exits `0` (at baseline).

### Edge cases

- A new test file added to `tests/integration/` with a basename that
  already exists in `tests/unit/` (e.g. a synthetic
  `test_synthetic_dup.py` placed in both) is still *collectible*
  (G1/G2 hold regardless of count) but pushes the ratchet lint's count
  to 7, exceeding the baseline of 6 — the lint fails, surfacing the
  problem locally before push instead of at a future accidental
  full-tree collection.
- Sub-packages: `tests/unit/` and `tests/integration/` are currently
  flat (no nested test directories); this spec does not add
  `__init__.py` to any directory beyond the three named, since none
  exist.

### Failure scenarios

- Pre-change code (no `__init__.py` anywhere under `tests/`):
  `pytest --collect-only tests/unit/test_review_dev_runner.py
  tests/integration/test_review_dev_runner.py` raises `import file
  mismatch` and aborts collection — this is the bug this spec fixes.
- A PR that introduces a 7th colliding basename: `python scripts/
  lint_no_duplicate_test_basenames.py --summary` exits non-zero,
  blocking the local pre-commit hook.

## Acceptance Criteria

- [ ] AC1: unit — with `__init__.py` present in `tests/`,
  `tests/unit/`, and `tests/integration/`, a subprocess-driven pytest
  collection of the two colliding files
  (`tests/unit/test_review_dev_runner.py` and
  `tests/integration/test_review_dev_runner.py`) together exits `0`
  with no `import file mismatch` in its output; this must FAIL on
  pre-change code (no `__init__.py`), where the same invocation exits
  non-zero with that exact message in stdout/stderr.
- [ ] AC2: unit — a subprocess-driven `pytest --collect-only tests/`
  (the bare `testpaths` default) exits `0` with no collection errors;
  must FAIL on pre-change code.
- [ ] AC3: unit — `repoach.lint.no_duplicate_test_basenames.scan`
  detects a synthetic duplicate basename between two temporary
  directories (files created via `tmp_path`, not the real repo tree)
  and returns it; asserts `scan` returns `[]` when the two temporary
  directories share no basenames. This must FAIL on pre-change code
  because the module does not yet exist (`ModuleNotFoundError`).
- [ ] AC4: unit — the ratchet test asserts
  `len(scan(Path("tests/unit"), Path("tests/integration"))) <=
  MAX_DUPLICATES` with `MAX_DUPLICATES = 6` (the current baseline);
  must FAIL on pre-change code (module does not exist).
- [ ] AC5: promised tests —
  `tests/unit/test_test_tree_packaging.py::test_collecting_colliding_basenames_together_succeeds`,
  `tests/unit/test_test_tree_packaging.py::test_bare_tree_collection_succeeds`,
  `tests/unit/test_no_duplicate_test_basenames_gate.py::test_scan_detects_synthetic_duplicate`,
  `tests/unit/test_no_duplicate_test_basenames_gate.py::test_scan_returns_empty_for_disjoint_basenames`,
  `tests/unit/test_no_duplicate_test_basenames_gate.py::test_duplicate_basename_count_does_not_exceed_baseline`.
- [ ] AC6: OPERATOR-MANUAL follow-up recorded in the PR body — the
  operator adds a `python scripts/lint_no_duplicate_test_basenames.py
  --summary` step to `.github/workflows/ci.yml` (bot-forbidden path);
  the bots do not touch that file.
- [ ] AC7: `ruff` + `ruff format --check` + `pytest tests/unit` +
  `pytest tests/integration` green (both suites, run separately as CI
  currently does, and together via AC2); zero inline comments
  (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`.

## Architecture Impact

- Adds/Removes dependency: none — three new empty `__init__.py` files
  (no import-graph edges), one new leaf module
  `src/repoach/lint/no_duplicate_test_basenames.py` under the existing
  `repoach.lint` package (sibling of `no_inline_comments` /
  `no_silent_except`, no cross-owner coupling), one new `scripts/*.py`
  CLI wrapper, and one additional block appended to the existing
  `.githooks/pre-commit`.
- New / changed coupling, cycles, or shared state: none — the new lint
  module has no dependency on `no_inline_comments.py` or
  `no_silent_except.py` beyond following the same shape.

## Diagram

N/A (packaging + a new leaf lint module; no control-flow change to
existing code).

## Open Questions

(none)
</spec_markdown>
