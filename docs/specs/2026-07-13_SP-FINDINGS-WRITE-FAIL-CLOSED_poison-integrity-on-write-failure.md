---
id: SP-FINDINGS-WRITE-FAIL-CLOSED
title: A failed findings write must not leave a clean review-integrity row
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

# A failed findings write must not leave a clean review-integrity row

## Intent

Prevent a transient DB/transport error during findings recording from
producing a head that looks fully reviewed with zero findings — which
the gate reads as APPROVE. Bind the review-integrity row to the
success of the findings write: if findings recording fails, do not
record a clean integrity row.

## Context

In the orchestrator outcome-recording path
(`src/ferova/review/orchestrator.py:434-480`),
`record_findings_for_outcomes` runs inside its own
`try/except Exception: log.warning`
(`orchestrator.py:434-454`) and `record_review_integrity` runs inside
a SEPARATE `try` block (`orchestrator.py:456-480`) that succeeds
independently. So a transient failure of the findings write is
swallowed to a warning while the integrity row is still written —
yielding a head_sha with a "complete review" integrity record
(`n_unparsed == 0`, `n_reviewers >= _MIN_REVIEWERS`) and ZERO
findings. `_assemble_facts` (`merge_gate.py:312-315`) then reports
`review_complete = True` with `open_blocking_findings = 0`, and
`verdict_from_facts` returns APPROVE. The review-complete signal
without its findings is a fail-open: absence of findings is
indistinguishable from "reviewed and clean".

Audit 2026-07-13 finding H3. Execution: hand-implement with human
review (audit 2026-07-13) — merge-path change.

## Goals

- G1: if `record_findings_for_outcomes` raises, the integrity row for
  that head is EITHER not written OR written in a POISONED form that
  the gate treats as an incomplete review.
- G2: a head with a failed findings write can never yield APPROVE
  from the merge facts.
- G3: the successful path is unchanged — findings recorded, then a
  clean integrity row, then APPROVE-eligible as today.

## Non-Goals

- NG1: no retry / backoff of the findings write here (a separate
  concern); the fix is purely to fail closed when it does fail.
- NG2: no change to `verdict_from_facts` thresholds.
- NG3: no schema change if the "skip the integrity row" option is
  chosen; a "poisoned" marker option MAY add a nullable column /
  sentinel `n_unparsed` value — pick one and document it.

## Assumptions

- A1: `record_review_integrity` and `record_findings_for_outcomes`
  write to the same ledger DB, so their success can be sequenced in
  one flow (record integrity only after findings succeed, or record
  integrity poisoned on failure).
- A2: the gate already treats a missing / incomplete integrity row as
  "review not complete" (`review_complete` is `any(... n_unparsed ==
  0 and n_reviewers >= _MIN_REVIEWERS ...)`), so skipping the row is a
  valid fail-closed implementation.

## Interface

N/A (in-place fix in the orchestrator recording path). If the
"poisoned row" option is chosen it adds no public signature change —
it sets integrity fields (e.g. a non-zero `n_unparsed` or a poisoned
flag) that make `review_complete` evaluate False.

## Behavior

### Nominal

Findings write succeeds → integrity row written clean (as today) →
head is APPROVE-eligible.

### Edge cases

- Findings write raises after partial insertion → the integrity row
  is not written clean; the gate sees no complete-review row for that
  head and `review_complete` is False.

### Failure scenarios

- `record_findings_for_outcomes` raises (DB locked / transport
  error) → integrity row skipped or poisoned → `MergeFacts` for that
  head has `review_complete = False` → `verdict_from_facts` does NOT
  return APPROVE. Fail CLOSED. The failure is still logged loudly
  (`findings_record_failed`).

## Architecture Impact

- Adds/Removes dependency: none — in-place modification of
  `orchestrator.py` (owned by an existing spec); no new cross-owner
  import. Couples the integrity write to the findings write outcome
  (sequenced within the existing recording block).
- New / changed coupling, cycles, or shared state: the integrity
  write now depends on the findings-write result within the same
  recording flow — an intended, local ordering, no new module edge.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — with a truthful boundary fake that makes
  `record_findings_for_outcomes` raise, the recording path does NOT
  persist a clean (`n_unparsed == 0`, `n_reviewers >= _MIN_REVIEWERS`)
  integrity row for that head.
- [ ] AC2 (INTEGRATION): run the orchestrator outcome-recording path
  end-to-end against an in-memory SQLite ledger with a boundary fake
  that forces the findings write to raise; then call the real gate
  (`gather_merge_facts` / `summarise_ledger_facts`) at that head and
  assert the resulting `MergeFacts` do NOT yield APPROVE
  (`review_complete == False`). Assert the success path still yields
  an APPROVE-eligible facts object.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_review_orchestrator.py::test_failed_findings_write_poisons_integrity`
  and `tests/unit/test_review_merge_gate.py::test_incomplete_review_not_approved`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit`
  green; zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no
  `# noqa`; `ferova arch graph --check` exits 0.

## Open Questions

OQ1: implement by hand + human review before re-trusting auto-merge
(audit) — this closes an APPROVE-on-empty fail-open.
