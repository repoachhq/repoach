---
id: SP-REVIEW-ROUND2-PARALLEL
title: Round-2 confirm-or-retract re-reviews run pooled, not sequential
version: 0.1
status: approved
author: jfaye (debt inventory 2026-07-22, LEVER-4)
created: 2026-07-22
updated: 2026-07-22

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Round-2 confirm-or-retract re-reviews run pooled, not sequential

## Intent

Round 1 of the review team fans the four reviewers out under a
`ThreadPoolExecutor`; round 2 (the confirm-or-retract dialogue for
reviewers that raised blocker/major comments) re-invokes the same
`review_diff` workload in a plain sequential `for` loop. On the
current degraded chains a single `review_diff` call costs 100-300 s,
so two or three triggered reviewers add 5-15 minutes of pure
serialization to every review cycle — and to every Coder re-review
round after it. Pool round 2 exactly like round 1.

## Context

- `src/repoach/review/orchestrator.py` —
  `ReviewTeamOrchestrator._round_two` (the sequential loop over
  `triggers`); round 1's pooled pattern lives in the same class
  (`ThreadPoolExecutor(max_workers=self._max_workers)` inside
  `review_pr`'s round-1 block).
- Loop iterations are already independent: the dialogue context is
  built from the frozen `round1_outcomes` + `guard_events` only, and
  results land by index into `revised[i]`. No cross-iteration state.
- Existing coverage: `tests/unit/test_review_round_two.py` (trigger
  selection, retract semantics, failure-keeps-round-1) must stay
  green unchanged.
- File ownership: `orchestrator.py` is owned by SP-ORCH-DOCSTRING
  (docstring-truth spec). If the module docstring describes round-2
  sequencing, it must be updated to stay truthful.

## Goals

- G1: all triggered round-2 re-reviews execute concurrently under a
  `ThreadPoolExecutor` capped by the same `self._max_workers` as
  round 1.
- G2: failure semantics unchanged — an exception in one re-review
  keeps that reviewer's round-1 outcome, logs
  `review_team.round_two_failed`, and never disturbs the other
  re-reviews.
- G3: result mapping is by reviewer index (`revised[i]`), independent
  of completion order.
- G4: per-reviewer `review_team.round_two_done` log events unchanged
  (role, verdicts, comment counts).

## Non-Goals

- NG1: no change to the trigger criterion (blocker/major comments).
- NG2: no change to dialogue-context content or prompt surfaces.
- NG3: no asyncio rewrite — thread pool only, mirroring round 1.
- NG4: comment/outcome posting stays as is (that is LEVER-6, a
  separate spec).

## Assumptions

- A1: `reviewer.review_diff` is thread-safe for concurrent calls
  across DISTINCT reviewer instances — already relied upon by
  round 1's pool over the same instances.
- A2: structlog emission from worker threads is safe — already
  exercised by round 1.

## Interface

No public signature changes. `_round_two(...) -> list[ReviewerOutcome]`
keeps its exact signature and return contract.

Inputs: unchanged.
Outputs: unchanged — one outcome per reviewer, round-1 outcome
returned unchanged for non-triggered reviewers.
Errors: none propagated (G2 — failures degrade to round-1 outcome).

## Behavior

### Nominal
- Compute `triggers` as today; empty → return `round1_outcomes`
  unchanged (early exit preserved).
- Submit one `review_diff` task per trigger to a
  `ThreadPoolExecutor(max_workers=self._max_workers)`; collect
  `(index, future)` pairs; assign `revised[i] = future.result()`.

### Edge cases
- Single trigger → pooled path still used (no special-casing); result
  identical to today's.
- More triggers than `max_workers` → executor queues them; all
  complete.

### Failure scenarios
- `future.result()` raises → log `review_team.round_two_failed` with
  the same fields as today, keep `round1_outcomes[i]`, continue with
  the remaining futures.

## Architecture Impact

- No edge added or removed: the change introduces no new
  cross-`owns` import (`concurrent.futures` is stdlib and already
  imported by the module for round 1).
- Shared state note: `revised` list written from the collecting
  thread only (futures resolved in the main thread), no new locking.

## Diagram

N/A — trivial slice (loop → pool of the same calls).

## Acceptance Criteria

- [ ] AC1 (concurrency proof, deterministic): a unit test in
  `tests/unit/test_review_round2_parallel.py` builds ≥2 triggered
  fake reviewers whose `review_diff` waits on a shared
  `threading.Barrier(parties=2, timeout=10)`; the round-2 pass
  completes with both re-run outcomes. Under the old sequential loop
  the barrier can never release, so the test discriminates: it fails
  (BrokenBarrierError) on the pre-change code.
- [ ] AC2 (failure isolation): one pooled fake raises; its reviewer
  keeps the round-1 outcome, the other triggered reviewer's round-2
  outcome lands, and `review_team.round_two_failed` is emitted for
  the failing role only.
- [ ] AC3 (index mapping): fakes completing in inverted order (event
  staggering, no sleeps) still land by reviewer index — verdicts are
  never cross-attributed.
- [ ] AC4 (regression): `tests/unit/test_review_round_two.py` passes
  unchanged.
- [ ] AC5 (cap): the executor is constructed with
  `max_workers=self._max_workers` (asserted via a capturing fake or
  code selector in the new test module).
- [ ] AC6 (integration): a test in
  `tests/integration/test_review_round2_parallel_flow.py` drives the
  public review entry through round 1 into round 2 with barrier-gated
  fake reviewers (2 triggered) and asserts the full flow completes
  with correct final outcomes — proving the pooled round 2 composes
  with round-1 pooling and outcome assembly, not just the isolated
  method. Fake reviewers follow the existing boundary-fake pattern of
  `tests/unit/test_review_round_two.py` (fakes at the LLM boundary,
  never monkeypatching orchestrator internals).

## Open Questions

None.
