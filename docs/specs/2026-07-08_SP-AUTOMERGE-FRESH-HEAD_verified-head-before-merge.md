---
id: SP-AUTOMERGE-FRESH-HEAD
title: Merge paths verify the API-served head against git ls-remote
version: 0.1
status: approved
author: jfaye (PR #50 orphaned-commit incident; verification dossier 2026-07-07; operator GO 2026-07-08)
created: 2026-07-08
updated: 2026-07-08

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Merge paths verify the API-served head against git ls-remote

## Intent

Never merge at a head GitHub's API happens to be serving — merge at
the head the git backend actually holds. Every merge-side resolution
gains a bounded API-vs-`ls-remote` convergence check that fails
CLOSED on mismatch, and the squash itself re-checks the tip one last
time after the gate decision.

## Context

Incident (2026-07-06, PR #50): the PR API served a `headRefOid` 40+
minutes stale; `run_auto_merge` evaluated every gate and squash-merged
at that stale head, ORPHANING a repair commit sitting on the real
branch tip (recovered by hand via cherry-pick, PR #51). Today every
merge-side head resolution trusts `gh pr view --json headRefOid` and
never cross-checks `git ls-remote origin refs/heads/<branch>` (ground
truth): `decide_at_head` (auto_merge.py:457), `evaluate_ci_gate`
(auto_merge.py:260), `run_auto_merge` → `squash_merge`
(auto_merge.py:671), `evaluate_merge_gate`, and `safe_merge.sh`
step 6. The existing `resolve_fresh_head` guard covers only the
review path, compares against the local checkout (not `ls-remote`),
and is advisory.

## Goals

- G1: A helper `resolve_verified_head(gh, pr_number, head_ref, *,
  repo_root, attempts, delay_s, sleep)` in
  `src/ferova/review/auto_merge.py`: resolves the real tip via
  `git ls-remote origin refs/heads/<head_ref>` and re-polls
  `gh.pr_head_sha` (bounded, injectable sleep) until the API head
  equals that tip; returns the converged SHA.
- G2: Fail-closed: on persistent mismatch, or when `ls-remote` itself
  fails, no SHA is returned — the reason carries both 12-char SHA
  prefixes.
- G3: `run_auto_merge` refuses with a new persisted outcome
  `OUTCOME_SKIP_STALE_HEAD = "SKIP_STALE_HEAD"` (recorded to
  `pr_merges` with both SHAs in notes) and never calls `squash_merge`
  when the head cannot be verified. When verification succeeds, the
  VERIFIED SHA feeds `evaluate_ci_gate(head_sha=...)` and
  `decide_at_head` (optional `head_sha` override) so gate facts are
  computed at the exact SHA about to be merged.
- G4: Immediately before `squash_merge`, the remote tip is re-read;
  if the branch moved after the gate decision the merge refuses with
  `OUTCOME_SKIP_STALE_HEAD` instead of merging.
- G5: `evaluate_merge_gate` performs the same verification: a stale
  head yields `decision.merge == False` with a "stale head" reason
  naming both SHAs — `ferova review gate` exits 5 and
  `safe_merge.sh` aborts.
- G6: `scripts/safe_merge.sh` compares the API `headRefOid` against
  `git ls-remote` between the gate step and `gh pr merge`, aborting
  on mismatch with both SHAs printed and NO emergency-override
  prompt (stale data is not overridable).

## Non-Goals

- NG1: No change to the review-side `resolve_fresh_head` or
  `test_review_head_guard.py` — the review path stays as is.
- NG2: No retry-until-forever: convergence polling is bounded by
  `attempts`; beyond it the merge is refused, not delayed.
- NG3: No GitHub server-side protection changes.

## Assumptions

- A1: `git ls-remote` against `origin` is ground truth for the branch
  tip and is available wherever merges run (CI runner and operator
  clone both have git + network to origin).
- A2: `pr_merges.notes` free-text can carry the SHA pair without a
  schema change.

## Behavior

### Nominal

API and `ls-remote` agree on the first try → verified SHA flows
through gate facts and the merge proceeds exactly as today.

### Edge cases

- API lags: re-poll up to `attempts` with `delay_s` — converges →
  proceed at the converged SHA.
- Persistent lag (the #50 case) → `SKIP_STALE_HEAD`, both SHAs in
  the outcome notes; the next `synchronize` event retries naturally.
- Branch moves between gate decision and squash (new push lands
  mid-merge) → the last-second re-read refuses; nothing merged.
- `ls-remote` transport failure → fail closed (refusal, not merge).

## Acceptance Criteria

- AC1: `tests/unit/test_automerge_fresh_head.py::test_verified_head_match_first_try`
  and `::test_verified_head_converges_after_repoll`.
- AC2: `::test_verified_head_persistent_mismatch_fails_closed` and
  `::test_verified_head_ls_remote_error_fails_closed` (reason carries
  both 12-char prefixes).
- AC3: `::test_auto_merge_refuses_on_stale_head_and_does_not_merge`
  (outcome `SKIP_STALE_HEAD` persisted, `squash_merge` never called).
- AC4: `::test_gate_facts_computed_at_verified_head`.
- AC5: `::test_auto_merge_refuses_when_head_moves_mid_gate`.
- AC6: `::test_evaluate_merge_gate_stale_head_refuses` (exit-5 path).
- AC7: `::test_safe_merge_script_contains_fresh_head_guard` (guard
  present in `scripts/safe_merge.sh`, ordered between the gate step
  and `gh pr merge`, no override prompt on this path).
- AC8: Existing suites stay green — `tests/unit/test_review_auto_merge.py`
  fixtures stub head verification to a match by default.

## Open Questions

- OQ1: `attempts`/`delay_s` defaults — proposal 4 × 30 s (covers the
  observed 40-min lag poorly on purpose: beyond ~2 min the right move
  is refuse-and-retry-on-next-event, not wait).
