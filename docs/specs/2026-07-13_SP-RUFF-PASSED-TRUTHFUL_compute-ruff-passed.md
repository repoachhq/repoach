---
id: SP-RUFF-PASSED-TRUTHFUL
title: Compute ruff_passed from a real session-level ruff result, never assert it
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Compute ruff_passed from a real session-level ruff result, never assert it

## Intent

`DevSessionResult.ruff_passed` is meant to report whether the finished
session's tree is ruff-clean. Today it is hardcoded to `True` after the
wrap-up pytest matrix without ruff ever running at session level, so it
lies whenever the tree has a lint violation and is stale on early-return
paths. Compute it from an actual ruff run, or remove the field if it is
genuinely redundant.

## Context

Audit 2026-07-13 finding M3. `src/ferova/review/dev_runner.py`:

- Line 1829: `result.ruff_passed = True` — set unconditionally right
  after `run_pytest_matrix(repo)` at line 1828, with no session-level
  ruff invocation preceding it.
- The field is stale on paths that return before self-verify (e.g. the
  early returns around the wrap-up repair at lines 1844-1849, and the
  step-failure return at 1823-1825 which never reaches line 1829).
- `run_ruff_gate` already exists and is imported/used elsewhere in the
  review package (e.g. `coder_findings.py:595`,
  `devagent_selfverify.py:289`) — the session can call the same gate on
  its tree.
- Note the per-step and self-verify ruff runs exist, but the
  SESSION-level `ruff_passed` field is what downstream consumers read;
  it must not be an unconditional assertion.

Runs inside `dev_runner` at the end of a Developer session. Review-
integrity change, not a merge-path change.

## Goals

- G1: `result.ruff_passed` reflects a real session-level ruff result
  over the working tree — `True` only when ruff (and format --check, to
  match the project gate) actually passed.
- G2: no code path leaves `ruff_passed` at a stale/optimistic default;
  a session that returns early records the truthful value known at that
  point (or `False` when ruff was never able to confirm clean).
- G3: if analysis shows the field is fully redundant with the
  self-verify ruff result, remove it and update its consumers instead
  of asserting it — but do not leave an unconditional `True`.

## Non-Goals

- NG1: no change to the per-step ruff gates or to `run_ruff_gate`
  itself.
- NG2: no change to `pytest_passed` computation.
- NG3: no new ruff configuration.

## Assumptions

- A1: `run_ruff_gate(repo)` returns `(ok, tail)` and is the sanctioned
  session-level ruff check (same helper the coder/self-verify paths
  use); calling it once at session wrap-up is inexpensive relative to
  the pytest matrix already run there.
- A2: at least one consumer reads `ruff_passed` (otherwise G3 applies);
  the spec's implementer confirms consumers before choosing compute vs
  remove.

## Interface

N/A (in-place fix). `DevSessionResult.ruff_passed` keeps its type; only
its source of truth changes (or the field is removed under G3).

## Behavior

### Nominal

Session completes all steps; wrap-up pytest matrix runs; a
session-level `run_ruff_gate(repo)` runs; `result.ruff_passed` is set
to its boolean result (clean tree → `True`).

### Edge cases

- Tree carries a ruff violation at wrap-up → `ruff_passed == False`
  (today it is wrongly `True`).
- Early return after a failed step (line 1823-1825) → `ruff_passed`
  carries the last truthful value / its non-optimistic default, never a
  stale `True` implying a clean tree that was never checked.

### Failure scenarios

- Ruff itself errors (non-lint failure) → treated as not-passed
  (`False`) with the tail logged; the session does not claim a clean
  tree it could not verify. Fail closed.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `dev_runner.py` (owned by an existing spec). Reuses the already-
  imported `run_ruff_gate`; no new cross-owner import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — a session wrap-up over a tmp tree with a deliberate
  ruff violation yields `result.ruff_passed is False`; a clean tree
  yields `True`.
- [ ] AC2 (INTEGRATION): drive a real Developer session wrap-up through
  the `dev_runner` entrypoint against a tmp git repo whose tree
  contains a genuine lint violation (real `run_ruff_gate` over real
  files, no monkeypatch) and assert the returned `DevSessionResult`
  reports `ruff_passed == False`; a clean-tree run reports `True`.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_dev_runner.py::test_ruff_passed_reflects_real_session_ruff`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
