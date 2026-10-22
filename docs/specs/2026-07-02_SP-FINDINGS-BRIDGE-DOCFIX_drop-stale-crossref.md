---
id: SP-FINDINGS-BRIDGE-DOCFIX
title: Drop the stale coder_loop cross-reference from _files_in_diff
version: 0.3
status: approved
author: jfaye (tech-debt survey 2026-07-02, ledger entry 10)
created: 2026-07-02
updated: 2026-10-22

owns:
  code: [src/repoach/review/findings_bridge.py, src/repoach/review/review_cache.py, tests/unit/test_review_pr_records_once_per_head.py]
  resources: []

depends_on: [SP-FINDINGS-INIT-RACE, SP-UNIVERSAL-CI-ENTRY, SP-UNIVERSAL-FORGE, SP-GATE-REVERIFIES-AT-HEAD, SP-ORCH-DOCSTRING, SP-REFUTER-JUDGES-WHAT-IT-CAN, SP-RETRY-BACKOFF-DEDUP]
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

## Amendment (0.3, 2026-10-22): the unit of a review is the run

Measured on yoob !306 with 0.2 (two consecutive `review pr --dry-run`,
`claude_code`): the second run printed `findings: 6 new, 0 already
recorded, 0 refuted earlier` and `refuter: judged 4`. The reviewers
rephrased every finding (claim similarity 0.15-0.42, cited lines moved
1-3), so no text fingerprint can identify a finding across two live
runs. The owner's decision: a head already reviewed is not reviewed
again. G4-G8 stay as the dedupe within one head (`--force`, two lenses
raising one claim).

- G9: a `review pr` run completes when every reviewer outcome is parsed
  (no `ERROR`) and its findings were written at a pinned head. It then
  stores one `pr_review_runs` row (run id = the run id stamped on its
  findings, pr, head, UTC completion time, verdict, and a payload with
  the reviewer outcomes after guard and round 2, the refutations, the
  spec fields), in the findings ledger.
- G10: when a completed run exists at `(pr, head_sha)` and `--force` is
  not given, `review pr` calls no reviewer and no refuter. It replays
  the stored outcomes and refutations, still runs the older-head re-check
  of SP-GATE-REVERIFIES-AT-HEAD G7 (no model call when nothing older is
  unsettled), derives the verdict from the ledger as a fresh run does,
  prints `review: cached at <sha7> (run <id>, <when>); use --force to
  re-review` on stderr, and returns the same JSON shape with
  `cached: true` (`false` on a fresh run).
- G11: a dry run and a posting run share the cache. A posting run
  replaying a cached run posts the stored outcomes through the forge's
  write barrier, as a fresh posting run posts its own.
- G12: `--force` runs the reviewers again; `repoach ci review` passes it
  only when `REPOACH_REVIEW_FORCE=1` (listed by `doctor --ci`).
- G13: a review without a pinned head (an orchestrator built without
  one) is never cached, since its head is known only after the
  reviewers ran.

- [x] AC12: a second `review pr --dry-run` at the same head makes 0
  reviewer calls and 0 judge calls, reports `cached: true`, the same
  verdict, reviews and findings as the first run, the same JSON keys,
  and the `review: cached at <sha7> (run …); use --force to re-review`
  line; the ledger is unchanged.
- [x] AC13: `--force` at the same head runs the reviewers, reports
  `cached: false` and `findings: 0 new, 1 already recorded, 1 refuted
  earlier`, and makes no judge call.
- [x] AC14: a posting run at a cached head posts the stored outcomes
  (same roles, same comments) without calling a reviewer or the judge.
- [x] AC15: a review at a new head reports `cached: false` and records
  its findings as new.
- [x] AC16: the `ci review` child gets `--force` with
  `REPOACH_REVIEW_FORCE=1` and not with any other value.
- [x] AC17: on yoob !306 the second consecutive `review pr --dry-run`
  makes 0 model calls. Measured 2026-10-22: run 1 `usage: 8 calls`,
  `findings: 6 new`, `refuter: judged 4`, `APPROVE`; run 2 `review:
  cached at 24b8111 (run e09f7faccb1c, …)`, `usage: 0 calls`, `APPROVE`,
  `cached: true`.

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

- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-FINDINGS-INIT-RACE (0.2: owner of `findings.py`; the bridge calls `mark_finding_seen`, the tests read and seed the ledger)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-UNIVERSAL-FORGE (0.2: the tests drive a `GitHubForge` with a real `git`)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-GATE-REVERIFIES-AT-HEAD (0.2: the tests route the older-head re-check to the stub judge)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-ORCH-DOCSTRING (0.2: owner of `orchestrator.py`; the tests drive `run_review`)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-REFUTER-JUDGES-WHAT-IT-CAN (0.2: the tests route the refuter pass to the stub judge)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-RETRY-BACKOFF-DEDUP (0.2: owner of `reviewer.py`; the tests build `ReviewerOutcome` stubs)
- Adds dependency: SP-FINDINGS-BRIDGE-DOCFIX -> SP-UNIVERSAL-CI-ENTRY (0.3: the tests drive the `ci review` child runner and read `FORCE_VAR`)
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
