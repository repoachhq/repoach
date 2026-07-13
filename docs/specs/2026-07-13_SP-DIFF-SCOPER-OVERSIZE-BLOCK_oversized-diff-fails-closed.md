---
id: SP-DIFF-SCOPER-OVERSIZE-BLOCK
title: An oversized omitted diff file must record a fail-closed blocking finding
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

# An oversized omitted diff file must record a fail-closed blocking finding

## Intent

`scope_diff` drops any per-file diff larger than the cap and tells the
bots "do not guess" — but nothing downstream forces a blocking outcome,
and the merge gate only needs a complete review plus CI. So padding a
hostile change past the cap removes it from all four review lenses while
keeping the PR mergeable. An oversized/omitted file must record a
fail-closed blocking marker so the gate cannot pass with unreviewed
files.

## Context

Audit 2026-07-13 finding M11.

- `src/ferova/review/diff_scoper.py` `scope_diff` (lines 75-122): a unit
  with `unit.chars > cap_chars` is appended to `oversized` and `omitted`
  (lines 101-103) and never shown; the announcement (line 114) tells the
  bots the omitted files are "NOT visible (do not guess about them)".
  `ScopedDiff` carries `oversized`/`omitted` (lines 117-122) but nothing
  converts them into a blocking finding.
- The merge gate (`merge_gate.py` `compute_merge_decision`, lines
  107-143) passes when the review is complete, CI is green, and no
  blocking finding survives re-verification — an omitted-because-
  oversized file is invisible to all of that, so the PR stays mergeable
  with unreviewed content.

`scope_diff` feeds every reviewer lens. This is a merge-path-relevant
integrity gap (it lets unreviewed code merge). Execution: hand-implement
with human review (audit 2026-07-13) — it changes what blocks a merge.

## Goals

- G1: when `scope_diff` omits a file because it is oversized, the review
  records a BLOCKING finding (a fail-closed marker in the findings
  ledger) naming the unreviewed file(s).
- G2: the merge gate REFUSES while any oversized-omitted blocking
  finding is open at head — an unreviewed file cannot be merged by
  padding past the cap.
- G3: the blocking finding clears the normal way once the file is
  reviewable (split/shrunk below the cap, so it is shown and no longer
  oversized) — no permanent wedge.

## Non-Goals

- NG1: no raising of `cap_chars` or streaming of huge diffs into the
  prompt — oversized files stay omitted from the prompt; the fix is the
  blocking record, not showing them.
- NG2: no change to the greedy packing of normally-omitted (cap-fit but
  budget-exceeded) files — only the OVERSIZED case becomes blocking
  (a genuinely enormous single file cannot be reviewed and must block).
- NG3: `scope_diff` itself stays a pure function; the finding is
  recorded by its CALLER from the `oversized` list it already returns.

## Assumptions

- A1: `ScopedDiff.oversized` already names every file dropped for size;
  the caller that runs the reviewers has the PR number and ledger
  handle to record a finding.
- A2: a `spec_gap`/`broken_behavior`-class BLOCKING finding recorded
  against an oversized file is honored by `gather_merge_facts`
  (its claim type is in the gate's blocking partition), so it counts as
  an open blocking finding at head until resolved.
- A3: recording the finding at review time (where the `ScopedDiff` is
  produced and the ledger is written) is the sanctioned injection point,
  keeping `diff_scoper` pure.

## Interface

N/A for `scope_diff` (unchanged pure signature). The reviewer
orchestration caller gains a step: for each path in
`ScopedDiff.oversized`, record a blocking finding via the existing
findings-recording API (in-place addition, no new public signature).

## Behavior

### Nominal

No oversized file → no oversized-blocking finding → gate behaves as
today.

### Edge cases

- A single file diff exceeds `cap_chars` → it is omitted from the
  prompt (as today) AND a blocking finding is recorded naming it; the
  merge gate reports it as an open blocking finding and refuses.
- Several oversized files → one blocking finding per file (or one
  finding listing all), each keeping the gate blocked.
- A file that is merely budget-omitted (fits the cap but the running
  total was exceeded) → NOT blocking (it can still be split across the
  review; only truly-oversized-single-file is unreviewable).

### Failure scenarios

- Hostile PR pads a real change past `cap_chars` to hide it → the
  oversized-blocking finding fires and the gate refuses; the change
  cannot merge unreviewed. Fail closed on unreviewable files.

## Architecture Impact

- Adds/Removes dependency: none new across owners — `diff_scoper.py`
  stays pure and unchanged in signature (owned by an existing spec); the
  blocking-record step is added to the reviewer orchestration caller
  (also owned by an existing spec), using the already-present findings
  API. No new owned module.
- New / changed coupling, cycles, or shared state: the reviewer caller
  now writes a findings-ledger record from the `ScopedDiff.oversized`
  list — a new intended data flow into the ledger the merge gate already
  reads, no cycle.

## Diagram

N/A (in-place fix)

## Acceptance Criteria

- [ ] AC1: unit — `scope_diff` still returns the oversized file in
  `oversized`/`omitted` (unchanged); the caller's recording step, given
  a `ScopedDiff` with a non-empty `oversized`, records a BLOCKING
  finding naming each oversized path.
- [ ] AC2 (INTEGRATION): drive the review path that scopes a PR diff
  containing one file larger than the cap, writing to an in-memory / tmp
  SQLite findings ledger, then run `gather_merge_facts` +
  `compute_merge_decision` at head. Assert an open blocking finding
  exists for the oversized file AND `merge is False`. A companion case
  with only cap-fit files records no oversized-blocking finding and the
  gate is not blocked by this rule.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_diff_scoper.py::test_oversized_file_records_blocking_finding`
  and
  `tests/unit/test_merge_gate.py::test_oversized_omitted_file_blocks_merge`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

- OQ1: implement by hand + human review before re-trusting auto-merge
  (audit) — this changes what blocks a merge.
