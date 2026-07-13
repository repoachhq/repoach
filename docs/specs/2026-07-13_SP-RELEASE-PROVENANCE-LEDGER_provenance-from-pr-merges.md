---
id: SP-RELEASE-PROVENANCE-LEDGER
title: Verify release provenance against the pr_merges ledger, not a forgeable subject suffix
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

# Verify release provenance against the pr_merges ledger, not a forgeable subject suffix

## Intent

The release gate accepts any commit in `main..develop` whose subject
ends in `(#N)` as a gated-PR squash. That suffix is forgeable text: a
direct push titled `hotfix something (#99)` passes provenance with no
cross-check against real PR merge SHAs. Verify each commit against the
`pr_merges` ledger (and/or real merge SHAs), so an out-of-band commit
wearing a `(#N)` costume is flagged.

## Context

Audit 2026-07-13 finding M10. `src/ferova/review/release_gate.py`:

- Line 35: `_SQUASH_SUBJECT_RE = re.compile(r"\(#\d+\)$")` — matches the
  default squash subject suffix only.
- `classify_release_range` (lines 42-59) returns
  `[subject for subject in subjects if not _SQUASH_SUBJECT_RE.search(subject.strip())]`
  — provenance rests entirely on the textual suffix; a forged subject
  is accepted.
- The `pr_merges` table already records real squash-merge SHAs
  (CLAUDE.md: "Persistence: `pr_reviews` + `pr_coder_responses` +
  `pr_merges` tables"; `ferova review merge` writes it). The gate does
  not consult it.

The release gate guards the operator-only `develop -> main` release.
Execution: hand-implement with human review (audit 2026-07-13) —
merge-path (release-path) change.

## Goals

- G1: release-range provenance is verified against the `pr_merges`
  ledger / real merge SHAs — a commit is accepted as a gated-PR squash
  only when a matching `pr_merges` record (by SHA, and where available
  the PR number parsed from the subject) exists.
- G2: a commit with a `(#N)` subject that has NO matching `pr_merges`
  record is flagged as out-of-band and named in the refusal.
- G3: the classifier degrades safely when the ledger is unavailable —
  it does not silently pass everything; an unreadable ledger is a
  refusal reason, not a rubber stamp.

## Non-Goals

- NG1: the gate still never merges `main` itself (the module's
  code-shape guarantee, lines 15-19, stands) — only the provenance
  CHECK changes.
- NG2: no change to CI-green / remote-tip / PR-head facts already in
  `ReleaseFacts`.
- NG3: no new `pr_merges` schema; the fix reads the existing ledger.

## Assumptions

- A1: `pr_merges` records the merge-commit / squash SHA and the PR
  number for every bot-gated merge into `develop`; a legitimate release
  range therefore has, for each commit, a ledger record matching its
  SHA (preferred) or its `(#N)`.
- A2: the SHA is the strong key; the `(#N)` suffix is corroborating,
  not authoritative — a subject whose `(#N)` names a PR whose recorded
  merge SHA differs from the commit's SHA is out-of-band.
- A3: `classify_release_range` gains access to the ledger (a `pr_merges`
  reader / db path) via its caller; the pure-function shape is
  preserved by passing the ledger facts in, not by shelling out inside
  the classifier.

## Interface

`classify_release_range` (or a new sibling it delegates to) takes the
release-range commits as `(sha, subject)` pairs plus the set of
recorded `pr_merges` (SHA + PR number), and returns the subset that is
out-of-band. Signature change is additive/typed; the old
subjects-only entry point may remain as a thin shim used only where no
ledger is available (documented as weaker).

Inputs: `commits: list[tuple[str, str]]` (sha, subject),
`merged: set[str]` (recorded merge SHAs) or a `pr_merges` reader.
Outputs: `list[str]` out-of-band subjects (or `(sha, subject)`).
Errors: an unreadable ledger surfaces as an explicit refusal reason,
not a swallowed exception.

## Behavior

### Nominal

Every commit in `main..develop` matches a `pr_merges` record by SHA →
empty out-of-band list → clean provenance.

### Edge cases

- A commit with subject `hotfix something (#99)` whose SHA is NOT in
  `pr_merges` (and/or whose `(#99)` maps to a different recorded SHA) →
  flagged out-of-band and named in the refusal, even though its subject
  matches `_SQUASH_SUBJECT_RE`.
- A legitimate squash whose subject was manually edited to drop the
  `(#N)` but whose SHA IS in the ledger → accepted (SHA is
  authoritative) — the ledger check is strictly stronger than the
  regex.

### Failure scenarios

- `pr_merges` unreadable / empty when the range is non-empty → the gate
  refuses with a "provenance unverifiable" reason rather than accepting
  on the forgeable suffix. Fail closed.

## Architecture Impact

- Adds/Removes dependency: `release_gate` gains a read dependency on the
  `pr_merges` ledger (already produced by the merge machinery); in-place
  modification of `release_gate.py` (owned by an existing spec,
  SP-RELEASE-GATE). If the ledger reader lives in an already-owned
  module, import it directly; declare no new ownership.
- New / changed coupling, cycles, or shared state: release gate now
  reads the same `pr_merges` ledger the merge gate writes — an intended
  provenance coupling, no cycle.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — given commits and a set of recorded merge SHAs, a
  commit whose SHA is absent is returned out-of-band even with a `(#N)`
  subject; a commit whose SHA is present is accepted even without the
  suffix.
- [ ] AC2 (INTEGRATION): with an in-memory / tmp SQLite `pr_merges`
  ledger populated with real merge records, drive the release-provenance
  path end-to-end over a tmp git repo whose `main..develop` range mixes
  ledger-backed squashes and one forged `hotfix ... (#99)` commit with
  no matching record. Assert the forged commit is flagged out-of-band
  and the resulting release decision refuses; a range where every commit
  has a ledger record yields clean provenance.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_release_gate.py::test_forged_pr_suffix_without_ledger_record_is_out_of_band`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: implement by hand + human review before re-trusting the release
  gate (audit) — release-path provenance is operator-critical.
