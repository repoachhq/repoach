# Architecture analysis — 2026-07-24

Exposition-first analysis produced as the "architectural" half of the post-debt-
campaign exit deliverable, from a multi-modal sweep of the live proxy logs, this
week's agent traces, and the `src/repoach/` tree (review 21.6k LOC / llm_proxy
13.5k LOC), adversarially verified. It **proposes** structural changes; none is
queued for implementation without maintainer sign-off (per "expose before
executing"). The bug-fix / cleanup half is the drafted `2026-07-24_SP-*` specs.

## Headline

The codebase is structurally healthy after the 36-spec audit sweep. The
remaining architectural debt is **concentrated, not diffuse** — four god-modules
plus one performance ceiling. None is urgent; each is a good candidate for a
deliberate, isolated extraction pass between feature work, in the order below.

---

## 1. `review/dev_runner.py` — 1957-line god-module (highest fan-in) · effort L

**Observation.** `dev_runner.py` is the single largest and most-imported module
in `review/`. It owns branch/commit/push plumbing, lint+pytest selector gates,
wrap-up failure repair, per-step promise gating, attribution, and the session
loop — responsibilities that changed under *five* different audit specs this
week (RUFF-PASSED-TRUTHFUL, PROMISE-RENAME-RETIRE, CONSISTENCY-SWEEP, and two
via tests). Every one of those changes had to reason about the whole file.

**Proposal (extraction, not rewrite).** Split along the seams that already exist
as private-function clusters:
- `dev_git.py` — branch/ensure/commit/push plumbing.
- `dev_gates.py` — the lint + pytest-selector runners + wrap-up-failure repair.
- `dev_step_executor.py` — per-step execution + promise gating.
- `dev_runner.py` keeps the session orchestration that composes them.

**Blast radius.** High import fan-in, so a mechanical move risks the Planner
atomic-import deadlock (see `planner-atomic-refactor-deadlock` memory). Must be
one atomic PR (imports updated in the same commit), hand-driven or with a
carefully staged plan — this is exactly the class the autonomous Planner splits
badly. **Recommendation: schedule as a dedicated hand-planned extraction; do not
hand it to `repoach develop` as one spec.**

## 2. Review orchestrator — `review_pr` is a 470-line method in a 1156-line class · effort M

**Observation.** `ReviewTeamOrchestrator.review_pr` mixes the review pipeline
(fetch diff → fresh-head guard → spec auto-load → disagreement fetch → reviewer
fan-out → ledger record) with three GitHub-publishing strategies (batched /
per-comment / archive-upsert) and notification side effects (routine fire +
escalation dossier). Threading, GitHub I/O, and persistence are interleaved in
one scope.

**Proposal.** Two collaborators the orchestrator composes:
- `ReviewPublisher` — batched/per-comment posting + archive-comment upsert +
  report rendering.
- `ReviewNotifier` — routine + escalation-dossier firing.
Then extract `review_pr`'s numbered steps into named private methods, each unit-
testable in isolation. This is the **lowest-risk, highest-clarity** of the four
(the seams are already named in the method's own docstring) — **recommended
first.**

## 3. `Settings` — flat 50+-field god object · effort M · priority low

**Observation.** `core.config.Settings` is a flat bag threaded into
`services.py` by raw attribute access; policy clusters (breaker slow-strike,
dispatch budget, first-byte deadlines) are spread across unrelated fields.

**Proposal.** Introduce narrow `@dataclass(frozen=True)` policy value-objects
(`BreakerSlowPolicy`, `DispatchBudgetPolicy`, …) built once from `Settings` and
passed where today 6-8 raw fields are threaded. Non-urgent; **fold into the next
routing/config cleanup rather than as its own effort.**

## 4. Streaming failover fragmented across three oversized functions/files · effort L

**Observation.** The per-candidate failover decision lives partly in
`peek_for_content`, partly in `_stream_with_failover`, partly in the transport
error mapping — three files, no shared vocabulary, hard to unit-test the
decision independently of the SSE-iterator plumbing.

**Proposal.** A small typed `FailoverAttempt` / `StreamOutcome` dataclass shared
by `peek_for_content` and `_stream_with_failover`, so the decision logic is
testable without driving a real stream. This is the **natural prerequisite for
LEVER-1 below** — do it first, then hedging becomes a much smaller change.

## 5. LEVER-1 — sequential chain failover, no concurrent hedging · effort L · PERFORMANCE

**Observation (verified).** `_stream_with_failover` is still strictly
sequential: it fully drains one candidate's SSE stream to completion (or a
terminal error) before trying the next. With the opus/haiku chains currently at
~36%/33% error and ~14s average slow-completions (from `logs/` + session-start
digest), worst-case wall-clock is the *sum* of failed-hop timeouts.

**Proposal.** For chains with ≥2 known-healthy candidates (breaker/health
state already tracks this), launch the top N concurrently with a short stagger
(200-500ms) and take the first successful stream, cancelling the losers.
Bounded by the existing breaker so an already-tripped ref is never hedged onto.

**Trade-offs.**
- Win: cuts the sum-of-timeouts tail to roughly the fastest healthy hop —
  directly attacks the current degraded-chain latency.
- Cost: N× upstream token spend on the hedged prefix until first-good-wins
  cancels; provider rate-limit pressure; strict cancellation discipline needed
  (ties to finding #1, the `claude_code` subprocess-orphan leak — **that bug
  must be fixed first**, else hedging leaks processes).
- Risk: correctness of "first good stream" under partial output; must not double-
  emit. The `FailoverAttempt` state object (#4) is the enabling refactor.

**Recommendation.** Worth doing, but **gated**: (a) land finding #1 (orphan-kill)
first, (b) do the #4 state-object extraction, (c) ship hedging behind a setting,
shadow-first (log would-hedge decisions before acting), mirroring the slow-strike
shadow rollout. Not a single autonomous spec — a small arc.

---

## Recommended sequencing

1. **Now (drafted specs):** the 25 bug-fix/cleanup specs — independent, testable,
   most S/M. Includes finding #1 (orphan-kill) and #16 (extend the schema-race
   fix to the 9 sibling stores), both of which unblock later arch work.
2. **Next isolated pass (recommended order):** #2 orchestrator publisher/notifier
   → #4 failover state object → #5 LEVER-1 hedging (shadow-first).
3. **Deliberate, hand-planned:** #1 dev_runner split (atomic-import risk).
4. **Opportunistic:** #3 Settings policy-objects, folded into a config pass.

## Cross-cutting theme

Three of this week's live incidents (basename-collision test landmine, structlog
cache-order flake, taxonomy-constant drift) share one root: **a fix landed in one
place but the class of bug was never closed structurally.** The drafted specs
convert each into a structural close (shared helper / lint / fixture), which is
the higher-leverage move than the Nth point-fix.
