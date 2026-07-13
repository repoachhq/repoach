---
id: SP-SPEC-CONTRACT-BASE
title: Load the acceptance contract from the base ref, not the PR head
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

# Load the acceptance contract from the base ref, not the PR head

## Intent

A PR can weaken its own acceptance contract: the review job reads
`docs/specs/*.md` and `docs/plans/*.md` from the PR-head checkout, so
a diff that edits its own spec/plan (or plants an empty promised test)
lowers the bar it must clear. Load the acceptance contract from the
BASE ref (`develop`), and require the promised test to be non-trivial.

## Context

The review job runs with cwd = the PR-head checkout, so
`maybe_load_active_spec` (`orchestrator.py:254-267`) and, in the
coverage block, `load_plan` + `compute_spec_coverage`
(`orchestrator.py:541-557`) read the spec/plan AS MODIFIED BY THE PR.
`compute_spec_coverage` (`src/ferova/review/spec_gate.py:166-190`)
derives its `acceptance_selectors` from the head plan, so a PR that
edits `docs/plans/<id>.md` chooses the selectors it will be graded
against.

Two compounding weaknesses: (1) the Coder path whitelist does not
exclude `docs/specs` / `docs/plans`, so bot fixes could touch the
contract; (2) `promised_present` (`spec_gate.py:101-140`) is
presence-only — a `def <name>(` with an empty body satisfies it, so a
PR can add a hollow test to claim coverage.

Audit 2026-07-13 finding H5. Execution: hand-implement with human
review (audit 2026-07-13) — merge-path change.

## Goals

- G1: the acceptance CONTRACT (the spec's active status + the plan's
  promised selectors) is loaded from the BASE ref of the PR
  (`develop`), not the PR head — a PR cannot relax the selectors it
  is graded against.
- G2: the promised test's PRESENCE is still checked against the head
  (the PR must actually add the test), but the SELECTOR SET graded
  comes from the base contract.
- G3: a promised test must be non-trivial (non-empty body), so a
  hollow `def test_x(): ...` does not satisfy coverage.

## Non-Goals

- NG1: no change to how coverage is recorded / read by the gate
  (`record_spec_coverage`, `fetch_spec_coverage`).
- NG2: no attempt to semantically validate that the test EXERCISES the
  change — only that the promised body is non-trivial (not `pass` /
  ellipsis / empty).
- NG3: the Coder whitelist tightening for `docs/specs` / `docs/plans`
  is noted here but MAY land as its own follow-up; this spec's core is
  base-ref contract loading + non-trivial promise.

## Assumptions

- A1: the review job can read the base ref content (e.g. via a
  `git show develop:docs/specs/<id>.md` / `git show
  develop:docs/plans/<id>.md` boundary against the real repo, or a
  base checkout) without a network call beyond what the job already
  has.
- A2: the plan id resolves the same on base and head (the id is
  derived from the branch, not the file content).

## Interface

`load_plan` / `maybe_load_active_spec` gain a way to read from a
specified ref (a `ref: str = "HEAD"` parameter, or a sibling
`load_plan_from_ref`), so `compute_spec_coverage` grades against the
base selectors while `promised_present` still resolves file presence
against the head checkout. `promised_present` (or a companion
predicate) additionally rejects a trivial promised body. Signatures
gain a ref parameter; the presence predicate gains a non-trivial-body
check.

## Behavior

### Nominal

Contract (spec active-status + plan selectors) loaded from base
`develop`; each base selector's file presence + non-trivial body
checked against the PR head. Coverage `covered` is True only when
every BASE selector is present-and-non-trivial at head.

### Edge cases

- PR edits `docs/plans/<id>.md` to drop or weaken selectors → the
  base plan is used → the dropped selectors are still required →
  coverage reflects the base contract.
- PR adds `def test_foo(): pass` (empty body) for a base selector →
  the non-trivial-body check fails → selector counts as NOT satisfied.
- Spec/plan absent on base (a brand-new spec introduced by this PR) →
  document the chosen policy: a genuinely new spec has no base
  contract, so coverage falls back to the head contract for
  first-introduction PRs ONLY, and this fallback is logged.

### Failure scenarios

- Base ref read fails → fail CLOSED: treat coverage as NOT covered
  (do not silently fall back to the head contract, which is the
  attackable path), and log the read failure.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `orchestrator.py` and `spec_gate.py` (owned by existing specs); no
  new cross-owner import. Adds a base-ref read (git boundary) at the
  coverage call site.
- New / changed coupling, cycles, or shared state: the coverage
  computation now reads two refs (base for the contract, head for
  presence) — an intended split, documented in the `compute_spec_
  coverage` docstring.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `promised_present` (or the companion predicate)
  returns False for a `def test_x(): pass` / ellipsis-only body and
  True for a real body; the base-ref plan loader returns the base
  selectors given a tmp git repo whose head plan differs from base.
- [ ] AC2 (INTEGRATION): in a tmp git repo with a `develop` base and
  a feature branch whose diff WEAKENS its own `docs/plans/<id>.md`
  (drops a promised selector), run the coverage computation the way
  the orchestrator does; assert `SpecCoverage.covered == False`
  because the BASE selector is graded and missing/weakened at head.
  Add a case where the head adds an empty-body test for a base
  selector and assert it does not satisfy coverage.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_spec_gate.py::test_coverage_graded_against_base_plan`,
  `::test_empty_body_promise_not_satisfied`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — a self-weakenable acceptance contract undermines every
spec-coverage gate decision.
