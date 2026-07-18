---
id: SP-GATE-ASYNC-DEF-SELECTOR
title: Promise-presence predicates must recognize async def tests
version: 0.1
status: approved
author: jfaye (PR #94 merge-gate blockage, 2026-07-18)
created: 2026-07-18
updated: 2026-07-18

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Promise-presence predicates must recognize async def tests

## Intent

Every factory predicate that scans source text for a test definition
matches only `def NAME(` — an `async def NAME(` test is invisible.
First live trigger: PR #94 (SP-CREDITS-CHECK), whose plan promises
three `httpx` tests in `tests/unit/test_credits.py` that are
`async def`. The tests exist and pass in CI, yet the review job
records `covered=False` (3 of 12 selectors "missing") and the merge
gate refuses with "spec acceptance selectors not all present". A
delivered, passing acceptance test must satisfy its promise
regardless of whether it is `def` or `async def`.

## Context

Three sites share the blindness, none aware of the `async` prefix:

1. `promised_present` (`src/ferova/review/spec_gate.py:139`) builds
   `(?m)^\s*def\s+NAME\s*\(` (SP-DEV-PROMISE-TRAILING-NAME,
   2026-07-10 — written before any promised test was async). It is
   the single presence predicate behind `selector_present`, so the
   blindness propagates to every consumer: the merge-gate coverage
   fact (`compute_spec_coverage`, recorded per head and read by
   `gather_merge_facts`), the Developer self-verify unit-missing
   check (`src/ferova/review/devagent_selfverify.py:277`), the
   Planner selector check (`src/ferova/review/planner.py:113-123`),
   and the dev-runner promise preflight
   (`src/ferova/review/dev_runner.py:498`).
2. `test_function_names` (`src/ferova/review/dev_runner.py:114`)
   collects delivered test names with
   `(?m)^\s*def\s+(test_\w+)\s*\(` for the mechanical-rename
   reconcile — an async delivered test is invisible, so the
   reconcile can conclude a promised test was never delivered.
3. The Coder placeholder guard (`is_placeholder_content`,
   `src/ferova/review/coder_loop.py:311`) rejects any test-file
   write with no `^\s*def\s+test_\w+` line as
   `test_file_no_tests` — a legitimate all-async test file emitted
   by the Coder is refused as a placeholder.

A repo-wide sweep for `def`-matching regexes over source text finds
exactly these three sites. The fix is the same at each: allow an
optional `async` prefix (`(?:async\s+)?def`). No DB migration is
involved: coverage records are recomputed by every review run, so
re-running the review on an affected PR heals its `covered` fact.

## Goals

- G1: `promised_present` satisfies a `file.py::test_name` promise
  when the file defines `async def test_name(` at any indentation,
  with the same word-boundary and class-tolerance semantics as the
  sync case.
- G2: `test_function_names` lists `async def test_*` functions
  exactly as it lists sync ones.
- G3: the placeholder guard accepts a test file whose only tests are
  `async def`.
- G4: the PR #94 shape passes end-to-end: a plan promising async
  selectors over a head that delivers them yields
  `SpecCoverage.covered == True`, and the merge decision carries no
  "spec acceptance selectors not all present" reason.

## Non-Goals

- NG1: no switch to AST parsing — the line-anchored regex family
  stays (it is deliberate: cheap, encoding-tolerant, and shared).
- NG2: no change to the selector grammar (`file.py`,
  `file.py::test`, `file.py::Class::test`, `[param]` stripping).
- NG3: no retro-editing of persisted `pr_spec_coverage` rows — the
  review run recomputes and re-records at head.

## Assumptions

- A1: an `async def` test is always collected by pytest in this repo
  (anyio/asyncio marker infrastructure already exists —
  `tests/unit/test_credits.py` passes in CI today).
- A2: `def` and `async def` are the only two definition forms a
  promised test can take.

## Interface

N/A (in-place fix — three regex literals widen; every signature,
caller, and data shape is unchanged). Docstrings that quote the old
pattern (`spec_gate.py` module + `promised_present`) are updated to
quote the widened one.

## Behavior

### Nominal

`async def test_x(` satisfies promise `f.py::test_x`; `def test_x(`
keeps satisfying it; both count in `test_function_names`; both
defeat the `test_file_no_tests` placeholder reason.

### Edge cases

- `async   def` (multiple spaces / tab) matches — the prefix is
  `(?:async\s+)?`.
- Word boundaries hold: promise `test_foo` is still NOT satisfied by
  `async def test_foobar(`.
- Class-nested `async def` at any indentation matches, as for sync.
- A commented-out or string-embedded `async def` line: same
  tolerance as the existing sync predicate (line-anchored scan, no
  semantic parse) — explicitly out of scope, unchanged.

### Failure scenarios

- File absent / unreadable → `False`/empty, exactly as today (no
  change to the error paths).

## Architecture Impact

- Adds/Removes dependency: none — three in-place regex edits inside
  `review/`-owned modules; no new imports, no ownership change
  (`owns.code` stays empty, mirroring SP-GATE-JUDGED-FAIL-CLOSED).
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — new file `tests/unit/test_promise_async_def.py`
  covering all three predicates with real files under `tmp_path`
  (no monkeypatching of ferova code):
  `test_promised_present_matches_async_def` (flat async def
  satisfies a `::test` promise),
  `test_promised_present_async_def_class_scoped` (class-nested,
  `Class::test` node id),
  `test_promised_present_async_name_boundary` (promise `test_foo`
  not satisfied by `async def test_foobar(`),
  `test_test_function_names_lists_async_defs` (mixed sync + async
  file lists both),
  `test_async_only_test_file_not_placeholder`
  (`is_placeholder_content` on a new tests/ path whose content has
  only `async def test_*` returns `is_placeholder=False`).
- [ ] AC2 (INTEGRATION): new file
  `tests/integration/test_gate_async_def_coverage.py::test_async_promises_yield_covered_and_gate_reason_free`
  — build a tmp repo tree delivering `async def` tests, compute
  `compute_spec_coverage` for a plan promising those selectors,
  assert `covered=True` / `missing=[]`; record it via
  `record_spec_coverage` into a tmp SQLite DB and assert the
  resulting merge facts produce a `compute_merge_decision` whose
  reasons do NOT include "spec acceptance selectors not all
  present".
- [ ] AC3: the three source regexes
  (`src/ferova/review/spec_gate.py`,
  `src/ferova/review/dev_runner.py`,
  `src/ferova/review/coder_loop.py`) accept the
  `(?:async\s+)?def` prefix, and the `spec_gate` docstrings quoting
  the pattern are updated in the same step.
- [ ] AC4: `ruff` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `pytest tests/unit` green — existing sync-promise tests
  (`tests/unit/test_spec_gate.py`,
  `tests/unit/test_dev_runner_promise.py`,
  `tests/unit/test_dev_growth_delta.py`) unchanged and green.

## Open Questions

None — after merge, re-run the review team on PR #94 so its
coverage fact is recomputed at head with the widened predicate
(operational step, outside this spec).
