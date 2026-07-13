---
id: SP-CLAIM-TYPE-PARTITION-ALIGN
title: Align judged-type partitions across gate and refuter; fail loud on unknown verdicts
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

# Align judged-type partitions across gate and refuter; fail loud on unknown verdicts

## Intent

The merge gate and the refuter disagree on which claim types are
"judged": the refuter verifies `spec_gap` but the gate does not count
it, so a refuter-VERIFIED blocking `spec_gap` lands in
`blocking_unverified` with a misleading "no verifier" reason instead of
blocking. Separately, claim-type routing picks its bucket by first-match
regex on prose, and an unknown reviewer verdict string is silently
coerced to a non-blocking `COMMENT`. Align the partitions from a single
source of truth and make both routing and verdict-parsing fail loud.

## Context

Audit 2026-07-13 findings M9 + reviewer unknown-verdict (low).

- `src/ferova/review/merge_gate.py` line 42:
  `_JUDGED_TYPES = frozenset({ClaimType.DESIGN, ClaimType.SECURITY})`.
  In `gather_merge_facts` a blocking finding whose type is neither in
  `_MECHANICAL_TYPES` (line 39-41) nor `_JUDGED_TYPES` is appended to
  `blocking_unverified` with `"... has no verifier"` (lines 207-210).
- `src/ferova/review/refuter.py` line 32:
  `JUDGED_CLAIM_TYPES = frozenset({ClaimType.DESIGN, ClaimType.SECURITY, ClaimType.SPEC_GAP})`
  — the refuter DOES judge `spec_gap`. So a refuter-verified blocking
  `spec_gap` is misclassified by the gate as unverifiable rather than
  counted as an open blocking finding.
- `src/ferova/review/findings_bridge.py` `classify_claim_type`
  (lines 59-81) routes a comment to a bucket by first-match over
  `_CLAIM_TYPE_CUES` regexes on the prose (lines 40-48) — e.g. a
  security comment containing the word "lint" is classified
  `LINT_CONVENTION`.
- `src/ferova/review/reviewer.py` lines 811-815: `ReviewVerdict(verdict_raw)`
  in a `try/except ValueError` that silently sets
  `verdict = ReviewVerdict.COMMENT` for any unknown/typo verdict (e.g.
  `"BLOCK"`), with no marker or warning — the parse module docstring
  (773-778) already promises a `[parse_failed:…]` marker for the
  missing-verdict case, but an UNKNOWN verdict slips past.

The gate is the merge authority; the refuter and bridge feed it.
Execution: hand-implement with human review (audit 2026-07-13) —
merge-path change (it moves what blocks a merge).

## Goals

- G1: the judged-type partition is a SINGLE SOURCE OF TRUTH shared by
  `merge_gate` and `refuter` (align on `{DESIGN, SECURITY, SPEC_GAP}`),
  so a refuter-VERIFIED blocking `spec_gap` at head COUNTS as an open
  blocking finding and blocks the merge — never `blocking_unverified`
  "no verifier".
- G2: claim-type routing is robust — a security-flavoured comment
  containing an incidental mechanical word (e.g. "lint") is not
  misrouted to a mechanical bucket by first-match-regex-on-prose.
- G3: an unparseable/unknown reviewer verdict fails LOUD — a
  `[parse_failed:…]`-style marker plus a warning — and is treated as a
  blocking/`REQUEST_CHANGES`-side outcome, never silently coerced to
  non-blocking `COMMENT`.

## Non-Goals

- NG1: no new claim types; the partition change is membership only
  (`spec_gap` moves into the judged set the gate honors).
- NG2: no replacement of the refuter judge or its persona.
- NG3: no full NLP classifier — G2 is satisfied by aligning the cue
  ordering/precedence with the mechanical-vs-judged partition (e.g.
  score/priority so an explicit security/design signal is not
  overridden by an incidental mechanical keyword), not by ML.

## Assumptions

- A1: aligning the gate's `_JUDGED_TYPES` with the refuter's
  `JUDGED_CLAIM_TYPES` (adding `SPEC_GAP`) is safe because the refuter
  already produces `verified`/`refuted`/`proposed` verdicts for
  `spec_gap`; the gate's judged-and-fresh rule (line 221-224) then
  applies uniformly.
- A2: `spec_gap` findings are stored with `checked_at_sha` so the
  gate's freshness check (`checked_at_sha == head_sha`) works for them
  exactly as for design/security.
- A3: a single shared constant (imported by both modules, or a small
  shared module) is the sanctioned single source of truth without
  creating a new owned module if it can live beside the existing
  `findings` types.

## Interface

N/A (in-place fix). `_JUDGED_TYPES` becomes an alias/import of the
shared judged-type set; `classify_claim_type` keeps its signature;
`reviewer._parse_response` keeps its return shape (an unknown verdict is
marked, not a new return type).

## Behavior

### Nominal

Design/security judged findings behave exactly as today. A parseable
known verdict routes as today.

### Edge cases

- Refuter-VERIFIED blocking `spec_gap`, fresh at head → counted in
  `open_blocking_findings`; the gate refuses the merge. It never lands
  in `blocking_unverified` as "no verifier".
- A security comment whose body incidentally contains "lint" → routed
  to `SECURITY` (the judged signal wins), not `LINT_CONVENTION`.
- Reviewer emits `"verdict": "BLOCK"` (unknown) → marked
  `[parse_failed:unknown_verdict:BLOCK]` (or equivalent), a warning is
  logged, and the outcome is treated on the blocking/`REQUEST_CHANGES`
  side — not silently `COMMENT`.

### Failure scenarios

- Any verdict string that does not map to a known `ReviewVerdict` →
  fail loud (marker + warning), consensus stays strict. Fail closed on
  unparseable verdicts.
- A blocking judged finding of any judged type that is PROPOSED at head
  still blocks via the existing PROPOSED path (lines 212-216) —
  unchanged.

## Architecture Impact

- Adds/Removes dependency: none new across owners — in-place
  modification of `merge_gate.py`, `refuter.py`, `findings_bridge.py`,
  `reviewer.py` (each owned by an existing spec). The shared judged-type
  set lives beside the existing `findings` claim-type definitions to
  avoid a new owned module; if a new tiny module is unavoidable it is
  called out here and kept import-only.
- New / changed coupling, cycles, or shared state: `merge_gate` and
  `refuter` come to share ONE judged-type constant instead of two
  drifting copies — a reduction in coupling risk, not an increase.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — the gate's judged-type set equals the refuter's
  (`{DESIGN, SECURITY, SPEC_GAP}`); `classify_claim_type` routes a
  security/design comment carrying an incidental mechanical keyword to
  the judged bucket; `reviewer._parse_response` on an unknown verdict
  returns a marked, non-`COMMENT` outcome with a warning path.
- [ ] AC2 (INTEGRATION): with an in-memory / tmp SQLite findings ledger,
  record a blocking `spec_gap` finding VERIFIED and fresh at head, then
  call `gather_merge_facts` + `compute_merge_decision` at that head —
  assert `merge is False` because the spec_gap counts as an open
  blocking finding (NOT surfaced as "no verifier"). A second
  integration case drives a reviewer outcome carrying an unknown verdict
  string through the parse path and asserts it is surfaced as a parse
  failure, not a silent `COMMENT`.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_merge_gate.py::test_verified_spec_gap_blocks_merge`
  and
  `tests/unit/test_reviewer.py::test_unknown_verdict_fails_loud_not_comment`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: implement by hand + human review before re-trusting auto-merge
  (audit) — this changes what the merge gate counts as blocking.
