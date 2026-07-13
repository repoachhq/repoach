---
id: SP-MISSING-TEST-VERIFIER-SCOPE
title: The missing-test verifier must search tests/ only
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

# The missing-test verifier must search tests/ only

## Intent

A "no test for `X`" finding is wrongly REFUTED because the verifier
greps both `tests/` AND `src/` and collects underscore-prefixed src
symbols — so the presence of the code-under-test in `src/` refutes the
claim that it has no test. Scope the missing-test verifier to `tests/`
and to `test_`-prefixed selectors.

## Context

`_verify_missing_test` (`src/ferova/review/finding_verifiers.py:44-53`)
extracts symbols via `_extract_missing_test_symbols`
(`src/ferova/review/hallucination_guard.py:257-289`), which collects
backticked ids that start with `test_` OR `_`
(`hallucination_guard.py:285-288`). It then searches with
`make_repo_symbol_searcher` (`hallucination_guard.py:513-556`), which
greps BOTH `tests/` and `src/` (`hallucination_guard.py:538`,
`for sub in ("tests", "src")`) for `def`/`class <symbol>`.

Result: the claim "no test for `_parse_verdict`" is REFUTED because
`def _parse_verdict` exists in `src/` (the function under review) —
the code's own existence refutes the missing-test claim. A PR can also
plant `def test_foo` in a whitelisted `src/` file to refute a
missing-test claim. Both are fail-open: a legitimate "no coverage"
finding is dismissed.

Audit 2026-07-13 finding H6. Execution: hand-implement with human
review (audit 2026-07-13) — merge-path change.

## Goals

- G1: the missing-test verifier searches `tests/` ONLY — the
  existence of a symbol in `src/` must NEVER refute a missing-test
  claim.
- G2: the search target is a `test_`-prefixed selector (the test that
  is claimed to be missing), not an underscore-prefixed src symbol
  (the code under review).
- G3: a genuine "no test for `X`" claim, with `X` present in `src/`
  and no corresponding test in `tests/`, is NOT refuted (stays
  VERIFIED / actionable).

## Non-Goals

- NG1: no change to `make_repo_symbol_searcher` for its OTHER caller
  (the general hallucination guard) — introduce a tests-only searcher
  for the missing-test path rather than changing the shared one's
  behaviour for everyone (or parameterise it), so no other verifier
  regresses.
- NG2: no attempt to prove a test EXERCISES the symbol — only that a
  test for it exists under `tests/`.

## Assumptions

- A1: the missing-test claim, when legitimate, names either the
  code-under-test (e.g. `_parse_verdict`) or the expected test name
  (e.g. `test_parse_verdict`); the verifier's job is to look under
  `tests/` for a covering test, not to confirm the src symbol exists.
- A2: a `test_`-prefixed selector is the correct search target; an
  underscore-prefixed src symbol is the code under review, not the
  thing whose absence is claimed.

## Interface

`_verify_missing_test` uses a tests-only symbol searcher (a new
`make_tests_symbol_searcher(repo_root)` restricted to `tests/`, or the
existing searcher parameterised with the subtrees to scan). Symbol
extraction for this path treats a `test_`-prefixed selector as the
search target and does not let a bare underscore-prefixed src symbol
be searched in `src/`. Signatures gain a subtree scope; no other
public contract changes.

## Behavior

### Nominal

Claim "no test for `_parse_verdict`", `def _parse_verdict` present in
`src/`, no test in `tests/` → the tests-only search finds nothing →
finding stays VERIFIED (the missing-test claim is confirmed).

### Edge cases

- A real test `def test_parse_verdict` exists under `tests/` → the
  tests-only search finds it → REFUTED (correctly — the test DOES
  exist).
- A PR plants `def test_foo` in a `src/` file → the tests-only search
  ignores `src/` → cannot be used to refute a missing-test claim.

### Failure scenarios

- No checkable target extracted → PROPOSED (unchanged — leave for a
  later round), never REFUTED by default. Fail CLOSED toward keeping
  the missing-test finding alive.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `finding_verifiers.py` and `hallucination_guard.py` (owned by
  existing specs); no new cross-owner import. Adds a tests-scoped
  searcher (or a scope parameter) alongside the existing one.
- New / changed coupling, cycles, or shared state: none — the general
  hallucination-guard searcher is untouched in behaviour for its own
  caller.

## Acceptance Criteria

- [ ] AC1: unit — the tests-only searcher returns False for a symbol
  defined only under `src/` and True for one defined under `tests/`;
  `_extract_missing_test_symbols` (as consumed by the missing-test
  path) yields the `test_`-prefixed target, not a bare src underscore
  symbol.
- [ ] AC2 (INTEGRATION): in a tmp repo with `def _parse_verdict` in a
  `src/` file and NO test under `tests/`, run `_verify_missing_test`
  on a Finding whose claim is "no test for `_parse_verdict`"; assert
  the returned status is VERIFIED (NOT REFUTED). Add the inverse:
  with a real `tests/.../test_parse_verdict` present, assert REFUTED.
  Add a case planting `def test_foo` in a `src/` file and assert it
  does not refute a missing-test claim.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_finding_verifiers.py::test_missing_test_not_refuted_by_src_symbol`,
  `::test_missing_test_refuted_by_real_tests_dir_test`,
  `::test_src_planted_test_does_not_refute`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Diagram

N/A (in-place fix)

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — a fail-open missing-test verifier dismisses legitimate
coverage findings before the gate ever sees them.
