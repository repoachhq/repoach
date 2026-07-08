---
id: SP-CI-FINDINGS-WIRE
title: Wire red CI into the findings ledger (slice 8b has no caller)
version: 0.1
status: approved
author: jfaye (live PR #53 post-mortem, 2026-07-07)
created: 2026-07-07
updated: 2026-07-07

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Wire red CI into the findings ledger (slice 8b has no caller)

## Intent

Make a red CI at head flow through the findings ledger like any other
defect, so the findings-driven Coder actually repairs it and the
review-time report stops asserting `CI green: True` over a failing
suite. The materialiser already exists —
`record_ci_failures_as_findings`
(`src/ferova/review/coder_findings.py:206`) — but a repo-wide search
shows it has ZERO callers. Slice 8b was implemented as a function and
never wired into the loop.

## Context

Observed live on PR #53 (2026-07-07):

- Both required checks failed at the head under review
  (`Test suite (Python 3.11/3.13)` — 12 test-stub `TypeError`s).
- The four reviewers passed with zero open blocking findings: the
  production diff is coherent; the defect lives only in CI.
- The `Coder auto-fix loop` job exited green in 4m36s WITHOUT pushing:
  the Coder consumes open findings (`coder_findings.py:141`), the
  ledger had none, and nothing materialised the red checks into it.
- `run_auto_merge` refused correctly (`SKIP_CI_FAILED`) via its
  independent CI gate — the safety net is not the problem.
- The sticky archive report rendered `Decision: MERGE-READY`,
  `CI green: True`: `summarise_ledger_facts`
  (`merge_gate.py:235`) derives `ci_green` from the ledger by design
  ("a red CI is materialised as a verified broken_behavior finding,
  slice 8b"), so an unwired materialiser makes the report blind to CI
  by construction.

Net effect: a PR whose only defect is a red suite reaches a stable
deadlock — reviewers approve, Coder no-ops, auto-merge refuses forever,
and the report actively misreports. No stuck escalation fires because
`stuck.py` counts findings across rounds and there are none.

## Goals

- G1: The review pipeline calls `record_ci_failures_as_findings` at a
  point where required-check conclusions for the head under review are
  available (after `fetch_ci_status` / before the Coder decides it has
  no work), passing the failed rows and per-check logs.
- G2: The findings-driven Coder consumes the resulting verified
  blocking `broken_behavior` findings and attempts the fix within the
  existing round/path-whitelist jail; `resolve_broken_behavior_findings`
  (`coder_findings.py:286`) closes them when the checks go green at the
  new head.
- G3: The archive report reflects reality: with a red required check at
  head and no fix yet, `summarise_ledger_facts` yields
  `ci_green=False` and the headline is not `MERGE-READY`.
- G4: A regression guard makes "implemented but unwired" loud: a unit
  test asserts `record_ci_failures_as_findings` is reachable from the
  coder-loop entry path (call-graph or behavioural test with a faked
  red rollup), so the wiring cannot silently regress.

## Non-Goals

- No change to `run_auto_merge`'s independent CI gate or its
  required-check polling — it behaved correctly and stays the
  authoritative refusal.
- No new claim types, no changes to verifier routing
  (SP-CLAIM-TYPE-ROUTING owns that).
- No attempt to have the Coder fix arbitrary CI failures beyond the
  existing jail (path whitelist, ≤ 3 rounds, ruff + pytest gate).

## Assumptions

- A1: The auto-review workflow job running the Coder has `gh` access to
  read check conclusions for the PR head (it already fetches CI status
  today via `coder_loop.fetch_ci_status`).
- A2: Idempotency of `record_ci_failures_as_findings` (live-claim
  dedup per check name, `coder_findings.py:242-258`) is sufficient for
  re-runs across synchronize events.

## Behavior

1. Coder loop entry, per round: fetch CI status for the head.
2. If required checks have failed conclusions: materialise them as
   verified blocking `broken_behavior` findings (with log blocks as
   verification result), then proceed — the Coder now has work even
   when reviewers raised nothing.
3. If all required checks are green at the current head: resolve open
   `broken_behavior` CI findings for this PR.
4. Report rendering needs no change: with the ledger fed,
   `summarise_ledger_facts` computes `ci_green` correctly as designed.

## Acceptance Criteria

- AC1: Re-running the review pipeline against a PR whose only defect is
  a failing required check yields ≥1 open blocking `broken_behavior`
  finding in `pr_findings` for that PR at that head.
- AC2: In that state the rendered archive report shows
  `CI green: False` and a non-MERGE-READY headline.
- AC3: The Coder job, given such a finding and a fixable failure inside
  the path whitelist, pushes a fix commit (observable on PR #53's
  class of failure: test-double signature drift).
- AC4: After the checks go green at the new head, the CI findings are
  resolved and the pure gate (`ferova review gate <N>`) reports
  `ci_green=True` with zero open blocking findings.
- AC5: A unit test fails if `record_ci_failures_as_findings` loses its
  caller again (grep-based call-graph assertion or behavioural test
  through the coder-loop entry with a stubbed red rollup).

## Open Questions

- OQ1: Should the reviewers' job ALSO materialise red CI (earlier
  signal in the archive), or is the Coder entry the single writer?
  Single-writer at Coder entry is the minimal change; the report only
  becomes truthful once the Coder job has run.
