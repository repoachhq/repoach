---
id: SP-CHAINPILOT-COLDSTART-HEALTHY
title: Cold-start only onto observed-healthy cells (structural anti-thrash)
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: N/A
  resources: N/A

depends_on:
  - SP-CHAINPILOT-PLAN   # amends select_cold_starts (3d-1c)

provides_to: []
constraints: {}
---

# SP-CHAINPILOT-COLDSTART-HEALTHY — never trial a model onto a cell that isn't working

## Intent
Fix-4 of four after the 2026-06-27 armed-autopilot incident — kills the
cold-start↔evict thrash. `select_cold_starts` resolved a candidate to a provider
cell via `resolve_provider_cell`, which *prefers* a healthy cell but **falls back
to any matching cell** when none is healthy. So `glm-5.2` and `qwen3.7-max` were
cold-started onto OpenRouter cells that had no healthy history, failed on the next
sweep, and were removed again — the insert↔remove oscillation.

## Context
The fix is one guard in `select_cold_starts`: after resolving the best cell, skip
the candidate unless that cell is in `healthy_cells` (≥`min_samples`
content-returning probes over the window). `resolve_provider_cell` keeps its
"best-effort cell" contract (used for its specificity/health-preference logic);
the health *requirement* lives in the cold-start selector.

This strongly damps thrash without a timer: a model healthy enough to be
cold-started (≥`min_samples` healthy in the window) cannot simultaneously be
unhealthy enough to be dropped/evicted (≥`min_samples`, dead-dominant) in the
*same* window — the two predicates are mutually exclusive per window, so the
incident's insert-then-immediate-remove (a never-healthy cell trialed then
removed) is eliminated. A cell sitting exactly on the `ok_fraction` boundary can
still flip across *adjacent* windows (e.g. 2-ok/2-dead → healthy → cold-started,
then one cycle later 1-ok/3-dead → dropped); that residual is a marginal cell and
is rate-limited by Fix-1's per-cycle cap, not the zero-history thrash this
removes. Combined with Fix-1 (≤N changes/cycle) and Fix-3 (≥2-providers to evict),
the loop converges instead of oscillating. No separate grace timer or audit-log
read is needed.

Exploration is preserved, only delayed: the cycle sweeps the **whole** matrix
(every provider × model, independent of chain membership), so a brand-new model
accrues a probe history while absent from the chains and becomes cold-start
eligible after ~`min_samples` healthy cycles (~18h at the 6h cadence). The gate is
an observation delay, never a block — there is no bootstrap deadlock.

## Goals
- G1: `select_cold_starts` only emits a trial when the resolved cell is in
  `healthy_cells`.
- G2: `resolve_provider_cell` behaviour is unchanged (still returns the best
  matching cell); the requirement is enforced by the selector.

## Non-Goals
- NG1: No explicit "recently cold-started" grace window / mutation-log lookup —
  the windowed-health mutual-exclusion already prevents the oscillation.
- NG2: Does not change placement, the cap (Fix-1), slow handling (Fix-2), or the
  fault scope (Fix-3).

## Assumptions
- A1: `healthy_cells` (built by `select_healthy_cells`, 3e, from the same windowed
  probes attribution uses) is the authoritative "currently works" signal.

## Interface
- `chain_plan.select_cold_starts` (behaviour: a health gate on the resolved cell).

## Behavior
- Candidate absent from chains + resolvable + cell healthy → cold-start trial.
- Candidate absent + resolvable but no healthy cell exists → skipped (no trial).
- Existing healthy-cell cold-start path unchanged.

## Architecture Impact
- Amends `chain_plan.py` (SP-CHAINPILOT-PLAN). No new import edge. `arch check`
  unchanged.

## Acceptance Criteria
- [ ] AC1: `select_cold_starts` with a resolvable but unhealthy-only cell
  (`healthy_cells` empty) emits no edits/mutations.
- [ ] AC2: with the cell in `healthy_cells`, the trial is emitted as before.
- [ ] AC3: ruff + format + no-inline + mypy(chain_plan) + `arch check` +
  full `pytest tests/unit` green.

## Open Questions
- Borderline (~`ok_fraction`) cells can still flip across adjacent windows. A
  hysteresis margin (a stricter ok-share to cold-start than to evict) would close
  that residual; deferred as a refinement since it is marginal and Fix-1 caps it.
- A latency-aware demote for persistently-slow heads remains the deferred Fix-2
  follow-up, unrelated to cold-start.
