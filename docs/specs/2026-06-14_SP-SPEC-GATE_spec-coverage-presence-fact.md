# SP-SPEC-GATE — record spec-coverage presence as a fact

## Metadata

- **Status**: OPEN
- **Priority**: P1 — review redesign slice 6 of 11
  (docs/review_redesign_architecture.md)
- **Owner**: operator
- **Executor**: `ferova develop`
- **Opened**: 2026-06-14

## Why

The redesign turns "the diff covers the spec" from a Tester opinion
into an executed fact. **Executing** the spec's acceptance criteria
(running the promised tests) belongs in the trusted merge context —
the review job is isolated from PR-code execution by
SP-CI-SECRETS-ISOLATION. This slice does the half safe in the review
job: it records whether the plan's promised acceptance selectors (each
step's ``unit_tests`` plus the plan's ``integration_tests``) are
**present** in the PR head — the file exists and, for a ``::test`` node
id, the symbol is defined. Combined with CI-green and the slice-7 gate
(which executes the selectors in the trusted merge job), present +
passing is the full coverage story. This catches the failure mode a
green CI hides: a PR that passes the existing suite without adding the
spec's promised criteria. Pure data-only reads; dual-run; no merge
decision changes.

## What

1. **New module `src/ferova/review/spec_gate.py`**:
   - `SpecCoverage(BaseModel)`: `spec_id`, `n_promised`, `n_present`,
     `missing: list[str]`, `covered` (true iff promised ≥ 1 and
     nothing missing).
   - `acceptance_selectors(plan) -> list[str]` — steps' `unit_tests` +
     `integration_tests`, de-duplicated in order.
   - `selector_present(repo_root, selector) -> bool` — the
     `file::node` file exists and, when a node id is given, the file
     defines `def <symbol>` (the `[param]` id is stripped). Data-only.
   - `compute_spec_coverage(repo_root, *, spec_id, plan) ->
     SpecCoverage`.
   - `pr_spec_coverage` table + `init_spec_coverage_schema`,
     `record_spec_coverage`, `fetch_spec_coverage` (self-contained
     engine, mirroring `findings.py`).
2. **Orchestrator wiring** — capture `spec.id` alongside the spec; after
   the refuter, if a plan is loadable
   (`load_plan(spec_id, root=self._repo_root)`), compute + record
   coverage, emitting `review_team.spec_coverage`. Any failure (no
   plan, hand-shipped PR) is logged as `spec_coverage_skipped` and
   never breaks the review.

## Files in scope

- `src/ferova/review/spec_gate.py` (new)
- `src/ferova/review/orchestrator.py` (wiring)
- `tests/unit/test_spec_gate.py` (new)

## Out of scope

- **Executing** the selectors (slice 7's pure gate, in the trusted
  merge job).
- `done_when` free-text and smoke prose that are not pytest selectors
  (the structured `unit_tests`/`integration_tests` are the executable,
  checkable contract).
- Any verdict/consensus/merge change (dual-run).

## Smoke scenario

A tmp head tree with a plan promising one unit selector (present) and
one integration selector (absent). `compute_spec_coverage` →
`covered=False`, `missing=[the absent selector]`, `n_present=1`,
`n_promised=2`; `record`/`fetch` round-trips it.

## Definition of Done

- Selector extraction de-dups in order — `test_acceptance_selectors_*`.
- Presence: file + symbol, absent file/symbol, bare file, stripped
  `[param]` id — `test_selector_present_*`.
- Coverage: fully covered, partial-when-promised-test-absent —
  `test_compute_coverage_*`.
- Ledger round-trip — `test_coverage_round_trip`.
- `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(review): spec-coverage presence module + pr_spec_coverage ledger`
2. `feat(review): orchestrator records spec coverage after judging`

## Risks

- **Presence ≠ passing**: by design — execution is slice 7 in the
  trusted context; CI-green covers passing meanwhile.
- **Hand-shipped PRs have no plan**: coverage is skipped (logged), not
  failed — correct for dual-run.
