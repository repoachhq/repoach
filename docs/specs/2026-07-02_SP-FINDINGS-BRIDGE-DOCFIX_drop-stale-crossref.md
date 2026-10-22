---
id: SP-FINDINGS-BRIDGE-DOCFIX
title: Drop the stale coder_loop cross-reference from _files_in_diff
version: 0.2
status: approved
author: jfaye (tech-debt survey 2026-07-02, ledger entry 10)
created: 2026-07-02
updated: 2026-10-22

owns:
  code: [src/repoach/review/findings_bridge.py, tests/unit/test_review_pr_records_once_per_head.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Drop the stale coder_loop cross-reference from _files_in_diff

## Amendment (0.2, 2026-10-22): a re-review at the same head records nothing twice

Measured (docs/tech_debt.md row 82, verified 2026-10-22): a second
`review pr` at an unchanged head inserted one more row per in-diff
comment, and a comment re-raising a finding refuted at that head
re-opened it to `proposed` (`_reopen_refuted_matches`), so the refuter
judged it again: `refuter: judged 8` on a second run that should judge
0. NG1 is withdrawn for this amendment; G4-G8 and AC5-AC11 pin it.

- G4: at a given head a finding is identified by its fingerprint
  (`finding_fingerprint`): SHA-256 over the claim type, the cited file,
  the line bucket (`line_start // 5`, the window `bench` buckets lines
  by) and the claim lowercased with punctuation dropped and whitespace
  collapsed. The reviewer is not part of it: two lenses raising the same
  claim are one finding. `bench`'s own fingerprint keys human labels by
  reviewer and leaves the claim out, so it is not reused.
- G5: recording a fingerprint a non-refuted row of the same head already
  carries inserts nothing; it stamps that row's `last_seen_at` (UTC
  ISO-8601) and `last_run_id` (one id per recording pass), two
  `pr_findings` columns added by the self-healing migration. Status,
  verification and claim are untouched. A second reviewer raising the
  same fingerprint in the same pass counts as already recorded.
- G6: a finding refuted at the reviewed head (its `checked_at_sha`, else
  its `head_sha`) is never re-opened by a later review at that head; a
  comment whose fingerprint matches only refuted rows of the head is not
  inserted either. Only a review at a new head re-surfaces it: the
  comment is recorded as new at that head, and a similar finding refuted
  at an older head is re-opened (SP-REFUTER-INJECTION-HARDEN G3) and
  re-checked at the new head by the older-head pass of
  SP-GATE-REVERIFIES-AT-HEAD G7.
- G7: `record_findings_for_outcomes` returns a `RecordedFindings` (new,
  already recorded, refuted earlier, re-opened, run id) instead of a
  count; `record_review_ledger` returns it, None when a write failed.
  `review pr` prints `findings: N new, M already recorded, K refuted
  earlier` on stderr and carries the three counts as
  `findings_recorded` in its JSON.
- G8: with no head (`head_sha` empty) nothing is matched: every comment
  is recorded, as before. `record_oversized_findings` applies the same
  fingerprint, so an oversized file is recorded once per head.

AC5-AC11 (`tests/unit/test_review_pr_records_once_per_head.py`, a real
git origin behind a forge with a stubbed `gh`, stub reviewers and a stub
judge):

- [x] AC5: two `review pr --dry-run` at the same head: the second inserts
  0 rows, makes 0 judge calls, prints
  `findings: 0 new, 1 already recorded, 1 refuted earlier`, leaves the
  refuted finding refuted and stamps a new `last_run_id` on the other.
- [x] AC6: a review at a new head prints
  `findings: 2 new, 0 already recorded, 0 refuted earlier` and records
  two rows at that head.
- [x] AC7: two reviewers raising one claim (case, punctuation, a line
  inside the bucket) in one pass record one row.
- [x] AC8: the fingerprint ignores the reviewer and separates lines of
  different buckets.
- [x] AC9: a finding refuted at the head stays refuted when re-raised
  there; one refuted at an older head is re-opened by a new head.
- [x] AC10: with no head every comment is recorded.
- [x] AC11: an oversized file recorded twice at one head yields one row.

## Intent

Make the `_files_in_diff` docstring in `findings_bridge.py` truthful
again: it claims to mirror a `coder_loop._files_in_diff` counterpart
"deliberately" as a "temporary duplicate", but no such counterpart
exists anywhere in the repository — the helper is the single
implementation. Stale prose misleads maintainers into hunting for a
duplicate that is not there.

## Context

`findings_bridge.py` converts reviewer comments into ledger Findings;
its `_files_in_diff` helper parses unified-diff headers for the
off-diff comment filter (SP-FINDINGS-OFFDIFF-FILTER). The 2026-07-02
tech-debt survey (docs/tech_debt.md entry 10) verified that
`coder_loop.py` contains no diff-header parser and never did in the
repo's visible history: the docstring's cross-reference and
"temporary duplicate" framing are stale, not a live duplication.
This spec also promotes `findings_bridge.py` from the ungoverned
frontier into the governed regime (opportunistic erosion per
SP-SPEC-TEMPLATE).

## Goals

- G1: The `_files_in_diff` docstring no longer references
  `coder_loop._files_in_diff` nor describes the helper as a temporary
  duplicate.
- G2: The failure-soft rationale stays documented: malformed input
  yields whatever could be parsed, an empty set is acceptable, and the
  caller then keeps every comment (historical no-filter behaviour).
- G3: A unit test pins the docstring invariant so the stale
  cross-reference cannot silently return.

## Non-Goals

- NG1: No change to any executable statement — this is a
  docstring-only slice; parsing behaviour is already pinned by the
  existing `findings_bridge` tests.
- NG2: No renaming, moving, or refactoring of `_files_in_diff`.
- NG3: No other docstring in the module is touched.

## Assumptions

- A1: `tests/unit/` and `tests/integration/test_findings_bridge.py`
  cover the helper's behaviour and stay green untouched.
- A2: `findings_bridge.py` has no owner in the arch registry
  (verified 2026-07-02: `Registry.owner_of` returns None), so this
  spec may claim `owns.code` without a disjointness conflict.

## Interface

Inputs: N/A (documentation-only change; the function signature
`_files_in_diff(diff: str) -> set[str]` is unchanged).

Outputs: N/A (unchanged).

Errors: N/A (unchanged).

## Behavior

### Nominal

The docstring describes what the helper does (walks `diff --git`,
`+++ b/`, `--- a/` headers, whitespace-tolerant, discards
`/dev/null`) and why it is failure-soft — with no reference to any
counterpart in another module.

### Edge cases

- N/A (documentation-only).

### Failure scenarios

- N/A (documentation-only).

## Architecture Impact

- 0.2: `findings_bridge.py` now calls `findings.mark_finding_seen`
  (SP-FINDINGS-INIT-RACE, already the owner of the ledger it writes).
- No edge added or removed. `findings_bridge.py` moves from the
  frontier into this spec's `owns.code`; its imports resolve to
  frontier files (reported, not enforced), so `depends_on` stays
  empty.

## Diagram

N/A (docstring-only slice).

## Acceptance Criteria

- [ ] AC1: `grep -n "coder_loop" src/ferova/review/findings_bridge.py`
  returns no matches.
- [ ] AC2: The `_files_in_diff` docstring still documents the
  failure-soft contract (malformed input -> parsed subset, empty set
  acceptable, caller keeps every comment).
- [ ] AC3: `tests/unit/test_findings_bridge_docfix.py` asserts the
  docstring invariants of AC1 + AC2 and passes.
- [ ] AC4: The full unit suite passes with no change to any
  non-docstring line of `findings_bridge.py`.

## Open Questions

(none)
