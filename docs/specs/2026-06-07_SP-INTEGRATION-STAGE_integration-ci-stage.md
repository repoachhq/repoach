# SP-INTEGRATION-STAGE — integration tests CI stage (builder slice)

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: `ferova develop` (autonomous — the builder ships
  its own final slice; `.github/workflows/ci.yml` is hand-shipped on
  the same branch because that path is bot-forbidden)
- **Opened**: 2026-06-07

## Why

Architecture decision #3 (test contract): integration tests are
promised per plan and must run in CI. `tests/integration/` exists
since SP-DEV-PLAN-EXEC (PR #324) with its first real test
(`tests/integration/test_developer_session.py`), but no gate runs it:
`scripts/ci_local.sh` only runs lint + `pytest tests/unit`, and the
GitHub workflow mirrors that. An unexecuted test contract is a
promise nobody checks.

## What

### 1. `scripts/ci_local.sh` — integration stage

Add one stage named `pytest tests/integration` that runs
`python -m pytest tests/integration -q` AFTER the existing
`pytest tests/unit` stage, following exactly the same bold/ok/fail
helper conventions already used in the script.

Behaviour rules:

- The stage runs in the DEFAULT (full) mode only. `--fast` and
  `--tests` keep their current meaning untouched (`--tests` stays
  unit-only).
- A new flag `--integration` runs ONLY the integration stage (mirrors
  the `--tests` pattern).
- When `tests/integration/` contains no test files, the stage prints
  a skip note and passes — an empty directory must not fail CI.

### 2. `tests/unit/test_ci_local_integration_stage.py` (new)

Unit tests that read `scripts/ci_local.sh` from the repo root and pin
the contract textually (same style as the repo's other script-reading
gate tests):

- the string `tests/integration` appears in the script;
- the `--integration` flag is declared in the usage/flag parsing;
- the `--fast` path does NOT invoke the integration stage.

### 3. `CLAUDE.md` — document the new mode

Update the "Local CI mirror" section: add the `--integration` line to
the command list, one line, same formatting as the existing flags.

## Files in scope

- `scripts/ci_local.sh`
- `tests/unit/test_ci_local_integration_stage.py` (new)
- `CLAUDE.md`

## Out of scope

- `.github/workflows/ci.yml` — bot-forbidden path; the matching
  workflow job is hand-committed on the same branch by the operator
  side. Do NOT emit fixes for it.
- Any change to the integration tests themselves or to the step
  executor.
- `--live` semantics, pre-commit/pre-push hooks.

## Smoke scenario

### Setup

Nothing — repo checkout only.

### Execute

```bash
scripts/ci_local.sh --integration
```

### Expected

Exit 0; output contains the integration stage banner and a green
summary; `pytest tests/integration` actually ran (1 test collected).

## Definition of Done

- `scripts/ci_local.sh --integration` exits 0 and runs exactly the
  integration suite.
- Full `scripts/ci_local.sh` (no flags) runs lint, unit AND
  integration stages, in that order.
- `scripts/ci_local.sh --fast` output does not change at all.
- `tests/unit/test_ci_local_integration_stage.py` passes and pins the
  three contract points above.
- `CLAUDE.md` lists the new flag.
- `bash -n scripts/ci_local.sh` clean; full `pytest tests/unit` green.

## Commit plan

1. `feat(ci): integration stage in ci_local.sh with --integration flag`
2. `test(ci): pin the ci_local integration-stage contract`
3. `docs: document --integration in CLAUDE.md`

## Risks

- Shell edits by the autonomous Developer: the per-step gates cover
  python only, so the textual contract tests in step 2 are the real
  gate — they must be strict enough to catch a broken stage wiring.
- The integration suite spawns real git subprocesses (~5 s) — fine
  locally and in CI, keep it out of `--fast`.
