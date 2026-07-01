# SP-DEV-RUFF-UNSAFE-FIXES — the build ruff gate applies ruff's unsafe fixes

## Metadata

- **Status**: OPEN
- **Priority**: P1 — a non-autofixable ruff nit (SIM102) stalled the
  SP-VERIFIER-LIB dispatch twice
- **Owner**: operator
- **Executor**: hand-implemented (touches the dispatch gate itself —
  circular to dispatch)
- **Opened**: 2026-06-13

## Why

`run_ruff_gate` runs `ruff check --fix` (safe fixes only). Rules like
`SIM102` (collapsible nested `if`) have a fix ruff marks **unsafe**, so
`--fix` leaves them; the Developer must hand-restructure, which the
chain models fail to do on retry — the SP-VERIFIER-LIB dispatch
stalled twice on exactly this. ruff itself knows the fix
(`if a: \n if b:` → `if a and b:`); it just needs `--unsafe-fixes`.
Applying it in the **build** gate kills the class, with the
promised-tests + full-suite matrix as the behaviour-safety net. The
Coder loop (fixing already-reviewed code) keeps the conservative
default.

## What

In `src/ferova/review/coder_loop.py` — `run_ruff_gate` gains a
keyword-only `unsafe_fixes: bool = False`; when `True`, step 1's
`ruff check --fix src tests` also passes `--unsafe-fixes`. Steps 2-4
unchanged.

In `src/ferova/review/dev_runner.py` — the `execute_plan_step`
ruff-gate call passes `unsafe_fixes=True`.

## Files in scope

- `src/ferova/review/coder_loop.py`
- `src/ferova/review/dev_runner.py`
- `tests/unit/test_ruff_gate_unsafe_fixes.py` (new)

## Out of scope

- Enabling `--unsafe-fixes` in the Coder loop (kept safe-only).
- Changing the ruff rule selection in `pyproject.toml`.

## Smoke scenario

A tmp repo with a module containing a collapsible nested `if`
(SIM102). `run_ruff_gate(repo, unsafe_fixes=True)` → green, the file
now reads `if a and b:`. `run_ruff_gate(repo)` (default) → red, tail
names SIM102.

## Definition of Done

- Build gate resolves SIM102 and reports clean —
  `test_build_gate_resolves_sim102_with_unsafe_fixes`.
- Default gate still fails on SIM102 (Coder unaffected) —
  `test_coder_default_leaves_sim102_unfixed`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): run_ruff_gate can apply ruff unsafe fixes`
2. `feat(dev): build gate applies unsafe ruff fixes (test-gated)`

## Risks

- **An unsafe fix changes behaviour**: caught by the per-step
  promised-tests and the end-of-run full-suite matrix; scoped to the
  build context only.
