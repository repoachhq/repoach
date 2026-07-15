---
id: SP-GATE-JUDGED-FAIL-CLOSED
title: Judged blocking findings that are OPEN or SHA-stale must fail closed
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

# Judged blocking findings that are OPEN or SHA-stale must fail closed

## Intent

Close two fail-open leaks in the merge gate that let a PR merge over
an unresolved judged (design/security) blocking finding: an OPEN
judged finding (a fix that did not resolve it) vanishes from the
count, and a still-confirmed judged finding keeps its original review
SHA forever so it goes stale after the first push. A judged blocking
finding that is OPEN or whose verification SHA is not the head must
count as BLOCKING.

## Context

`gather_merge_facts` (`src/ferova/review/merge_gate.py:175`) is the
authoritative re-verifying gate. For judged types it counts a finding
only when `finding.status in _BLOCKING_STATUSES` (VERIFIED, STUCK)
AND `finding.checked_at_sha == head_sha`
(`merge_gate.py:221-224`); `summarise_ledger_facts` applies the same
`status == verified and checked_at_sha == head_sha` rule
(`merge_gate.py:279-283`). Two leaks:

(a) OPEN leak. `open_verified_blocking`
(`src/ferova/review/coder_findings.py:92-123`) moves blocking
findings VERIFIED → OPEN when the Coder picks them up. A failed fix
leaves them OPEN. OPEN is not in `_BLOCKING_STATUSES`, and the judged
branch has no PROPOSED-style catch for it — an OPEN judged blocking
finding is neither counted in `open_blocking` nor added to
`blocking_unverified`. The gate merges over it.

(b) SHA-stale leak. `reverify_resolution_for_pr`
(`coder_findings.py:126-197`) refreshes `checked_at_sha` only on the
REFUTED → RESOLVED transition (`coder_findings.py:179-187`). A judged
finding the refuter STILL confirms stays OPEN with its original
review SHA — so after the next push `checked_at_sha != head_sha`, the
`== head_sha` guard drops it, and the gate merges over it.

Also finding M8: `_assemble_facts` (`merge_gate.py:294-326`)
head-pins review integrity to `head_sha`
(`merge_gate.py:312`, `fresh = [r ... if r["head_sha"] == head_sha]`)
but takes spec coverage from `coverage[-1].covered`
(`merge_gate.py:320`) — the LAST record at ANY head. A stale
`covered=True` from an earlier head carries the gate after a push
that regresses coverage.

Audit 2026-07-13 findings C2 (CRITICAL) + M8. Execution:
hand-implement with human review (audit 2026-07-13) — merge-path
change.

## Goals

- G1: a judged blocking finding whose stored status is OPEN counts as
  BLOCKING in both `gather_merge_facts` and `summarise_ledger_facts`
  (it is unresolved work, not an absence).
- G2: a judged blocking finding whose `checked_at_sha != head_sha`
  counts as BLOCKING (SHA-stale = unverified at head, fail closed).
- G3: spec coverage in `_assemble_facts` is pinned to `head_sha` —
  only a coverage record computed at the current head can satisfy
  `spec_covered`.

## Non-Goals

- NG1: no change to mechanical-type handling (they re-verify on disk
  at head already).
- NG2: no change to the refuter / re-verify LLM loop itself — G2 is
  enforced at the gate, independent of whether the re-verify SHA is
  refreshed. (Refreshing `checked_at_sha` on a still-confirmed judged
  finding is a valid alternative implementation of G2 and MAY be done
  in addition, but the gate must fail closed regardless.)
- NG3: no new tables or columns.

## Assumptions

- A1: `SpecCoverage` records carry the `head_sha` they were computed
  against (`record_spec_coverage`, orchestrator writes one per head)
  so `_assemble_facts` can select the head-matching record.
- A2: OPEN is a non-settled status; a finding in `_SETTLED` (resolved
  / refuted / obsolete) is correctly ignored and stays ignored.

## Interface

N/A (in-place fix — the `MergeFacts` shape and the two gate entry
points keep their signatures; only the counting predicates and the
coverage selection change).

## Behavior

### Nominal

Judged blocking finding VERIFIED/STUCK at head_sha → counts (as
today). Judged blocking finding OPEN (any SHA) → counts (fail
closed). Judged blocking finding VERIFIED but `checked_at_sha !=
head_sha` → counts (SHA-stale, fail closed). Judged finding in
`_SETTLED` → ignored. `spec_covered` is `True` only when a coverage
record exists FOR `head_sha` and its `covered` is `True`.

### Edge cases

- No coverage record at head_sha (only older heads) →
  `spec_covered = False`, `spec_coverage_known` reflects whether ANY
  head-matching record exists (a PR that never re-ran coverage at
  head is not "covered").
- OPEN mechanical blocking finding is unaffected — mechanical types
  re-verify on disk regardless of status.

### Failure scenarios

- Head advances after a judged finding was verified, with no
  re-verification → `checked_at_sha != head_sha` → BLOCKING → gate
  refuses. Fail CLOSED.
- A failed Coder fix leaves a judged security finding OPEN → BLOCKING
  → gate refuses. Fail CLOSED.

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `merge_gate.py` (owned by an existing spec); no new cross-owner
  import.
- New / changed coupling, cycles, or shared state: none — the gate
  reads the same ledger it already reads, with stricter predicates.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — seed an in-memory SQLite findings ledger; assert
  `gather_merge_facts` counts a judged BLOCKING finding as
  open_blocking when its status is OPEN, and when its status is
  VERIFIED but `checked_at_sha != head_sha`. Assert a settled
  (resolved/refuted) judged finding is NOT counted.
- [ ] AC2 (INTEGRATION): seed the ledger with a VERIFIED blocking
  SECURITY finding whose `checked_at_sha == old_head`; simulate a
  head advance by calling `gather_merge_facts` at a NEW `head_sha`
  (no re-verification recorded); assert the resulting `MergeFacts`
  yields `merge == False` (i.e. `open_blocking_findings > 0` and the
  verdict-from-facts decision is not APPROVE). Add a spec-coverage
  case: a `covered=True` record at `old_head` and none at the new
  head → `spec_covered == False`.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_merge_gate.py::test_open_judged_blocking_fails_closed`,
  `::test_stale_sha_judged_blocking_fails_closed`,
  `::test_spec_coverage_pinned_to_head`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — this is the final merge authority for judged findings.
