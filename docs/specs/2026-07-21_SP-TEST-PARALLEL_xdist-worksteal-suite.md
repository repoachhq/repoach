---
id: SP-TEST-PARALLEL
title: Parallelize the test suite with pytest-xdist worksteal
version: 0.1
status: approved
author: jfaye (perf review 2026-07-21; measured on the dev box)
created: 2026-07-21
updated: 2026-07-21

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Parallelize the test suite with pytest-xdist worksteal

## Intent

The unit suite (1885 tests) runs serial in 6m23s. Every full-suite
consumer pays it: each CI matrix leg, every `ci_local.sh` run, every
Coder fix-loop gate. Measured on the 8-core dev box (2026-07-21,
develop + SP-BREAKER-PROVIDER-SCOPE head):

- `-n auto` (default dist): 4m29s — the suite's long tail of slow
  tests defeats round-robin scheduling;
- `-n auto --dist worksteal`: **2m58s (2.15×)** — work stealing
  rebalances the tail.

Both parallel runs passed clean twice: the suite is already
isolation-safe. This spec wires the measured-best flags into the dev
dependency set and the local CI mirror.

## Context

- `pyproject.toml` `[project.optional-dependencies].dev` carries
  pytest / pytest-asyncio / pytest-cov but not pytest-xdist;
  `[tool.pytest.ini_options].addopts` is
  `-ra --strict-markers --strict-config` and must STAY serial-neutral
  (selector-sized runs — the Developer/Coder per-step gates — would
  only pay worker startup for nothing).
- `scripts/ci_local.sh` invokes the full suites in its `--tests`,
  `--integration` and full-parity paths.
- `.github/workflows/ci.yml` also runs the full suite, but workflow
  files are whitelist-forbidden in the factory lane — flipping that
  line is an OPERATOR follow-up outside this spec (see Open
  Questions).

## Goals

- G1: `pytest-xdist>=3.6` is a dev dependency; `python -m pytest
  tests/unit -n auto --dist worksteal` is green from a fresh
  `pip install -e ".[dev]"`.
- G2: every full-suite pytest invocation inside `scripts/ci_local.sh`
  carries `-n auto --dist worksteal`; selector-sized invocations
  elsewhere in the repo stay untouched.
- G3: `addopts` stays serial-neutral (no global `-n`).
- G4: the parallel suite is stable, not luckily green: three
  consecutive `-n auto --dist worksteal` runs of `tests/unit` and one
  of `tests/integration` pass during the implementing session.

## Non-Goals

- NG1: no `.github/workflows/ci.yml` change (factory-lane whitelist;
  operator follow-up).
- NG2: no parallelization of the Coder/Developer selector gates or
  the per-interpreter Coder matrix (`run_pytest`) — follow-up spec if
  the fix-loop latency warrants it; keeps this spec src-free.
- NG3: no test isolation rework — measured unnecessary; if a test
  proves parallel-unsafe during G4, fixing THAT test in place is in
  scope, rewriting suites is not.

## Assumptions

- A1: worker count on the runner is bounded by `-n auto` = cores;
  CI legs serialize on the single runner so full-width is safe.
- A2: pytest-cov composes with xdist (upstream-supported pairing).

## Interface

N/A (a dev dependency and shell-script flags; no Python API changes).

## Behavior

### Nominal

`scripts/ci_local.sh --tests` completes the unit suite in ~3 min on
8 cores with identical pass/fail semantics to the serial run.

### Edge cases

- A parallel-only failure (shared tmp path, env leak, port clash)
  surfaced by G4 is fixed in the offending test with standard
  isolation idioms (`tmp_path`, `monkeypatch`, per-test ports).
- `-p no:cacheprovider` interactions: none expected; cache stays on.

### Failure scenarios

- If xdist is absent (stale venv), ci_local.sh fails loudly with
  pytest's unknown-option error — acceptable; `pip install -e
  ".[dev]"` is the documented first command.

## Architecture Impact

- Adds dependency: `pytest-xdist` (dev-only). No production imports,
  no coupling, no shared state.

## Diagram

N/A

## Acceptance Criteria

- [ ] AC1: `pyproject.toml` dev extras include `pytest-xdist>=3.6`;
  new file `tests/unit/test_ci_parallel_pins.py` with
  `test_xdist_is_a_dev_dependency` (parses pyproject with tomllib and
  asserts the pin) and `test_ci_local_full_suite_runs_parallel`
  (reads `scripts/ci_local.sh` and asserts every `pytest tests/`
  full-suite invocation line carries `-n auto` and
  `--dist worksteal`).
- [ ] AC2: `scripts/ci_local.sh` full-suite pytest invocations carry
  `-n auto --dist worksteal`; its lint-only paths are unchanged.
- [ ] AC3: `[tool.pytest.ini_options].addopts` unchanged
  (`test_addopts_stays_serial_neutral` in the same test file pins
  the absence of `-n` in addopts).
- [ ] AC4: three consecutive `python -m pytest tests/unit -n auto
  --dist worksteal -q` runs green and one
  `python -m pytest tests/integration -n auto --dist worksteal -q`
  run green in the implementing session (recorded in the session
  log); `ruff` + `ruff format --check` green; zero inline comments;
  no `# noqa`.

## Open Questions

- OQ1 (operator follow-up, outside the factory lane): flip the
  `.github/workflows/ci.yml` full-suite line to
  `-n auto --dist worksteal` in a hand PR once this spec ships.
