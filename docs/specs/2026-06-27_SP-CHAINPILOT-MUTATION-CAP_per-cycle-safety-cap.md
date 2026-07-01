---
id: SP-CHAINPILOT-MUTATION-CAP
title: Per-cycle cap on chain-mutating edits (autopilot safety backstop)
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: N/A
  resources: N/A

depends_on:
  - SP-CHAINPILOT-PLAN   # caps inside plan_chain_rewrite (3d-1c)
  - SP-CHAINPILOT-LOOP   # threaded through plan_and_apply / run_autopilot_cycle (3e)

provides_to: []
constraints: {}
---

# SP-CHAINPILOT-MUTATION-CAP — the missing safety backstop

## Intent
The first of four fixes after the 2026-06-27 armed-autopilot incident. On the
first armed cycle the loop produced **401 mutations** and applied 13 in one shot,
gutting the chains to a single model + backstop (incl. evicting healthy heads).
The other three fixes correct *which* mutations are produced; this one bounds
*how many* can ever land in a cycle, so even a badly mis-firing attribution can
only nudge the chains a little per cycle — gradual, observable, reversible (the
`.bak`), never catastrophic. A guard named in the original 3d design that never
shipped.

## Context
`plan_chain_rewrite` maps the decision engine's mutations to structural edits and
applies them through `rewrite_chains`. The destructive edits (`evict_model` /
`drop_provider` / `demote` / `promote`) are first **filtered to models actually
in the chains** (a fault on a non-chain model is a no-op edit — it must not waste
the cap budget) and then truncated to `max(0, max_mutations)` before the rewrite;
cold-starts are already bounded separately by `per_tier`. Truncation keeps the
decision engine's deterministic order (the remaining mutations recur next cycle
if still warranted). The faithful journal (3d-2) records the capped-out mutations
as `applied=False`, since their edits never reach `rewrite.applied`.

`max_mutations` bounds the number of distinct in-chain MODELS mutated per cycle,
not ref-level changes: an edit is tier-agnostic, so one capped edit can still
strip a model from several slots (worst case `max_mutations` × the slots it is
in). The rewriter's never-empty guard keeps each chain's backstop, and the
attribution fixes (Fix-2/3) cut the mutation volume, so catastrophic gutting is
prevented in practice — this cap is the floor, not the whole guarantee.

The cap is a settings flag (`FEROVA_CHAINPILOT_MAX_MUTATIONS`, default **2**,
`ge=0`) threaded `run_autopilot_cycle → plan_and_apply → plan_chain_rewrite`; a
negative value is rejected by the Field and additionally clamped to 0 (fails
closed) at the truncation. The runtime `chains.env.bak` backup is gitignored.

## Goals
- G1: `plan_chain_rewrite(..., max_mutations: int = DEFAULT_MAX_MUTATIONS)` caps
  the mutation edits (not cold-starts) applied this cycle.
- G2: `DEFAULT_MAX_MUTATIONS = 2` exposed in `chain_plan`; `plan_and_apply` and
  `run_autopilot_cycle` default to it and thread the value through.
- G3: A `chainpilot_max_mutations` settings flag (`FEROVA_*`, default 2) the CLI
  passes into the cycle.

## Non-Goals
- NG1: Does NOT change which mutations the decision engine produces (Fix-2/3) nor
  cold-start selection (Fix-4) — only how many land.
- NG2: Does NOT prioritise *which* mutations survive the cap beyond the decision
  engine's existing deterministic order (a blunt safety bound; the rest recur).

## Assumptions
- A1: Applying a small number of mutations per cycle and letting the rest recur
  converges safely — the loop runs on a cadence, so a genuine fault is re-acted
  on next cycle.
- A2: Cold-starts are already bounded (`per_tier`), so capping only the mutation
  edits is sufficient to bound total per-cycle change.

## Interface
- `chain_plan.DEFAULT_MAX_MUTATIONS`, `plan_chain_rewrite(..., max_mutations=...)`.
- `chain_loop.plan_and_apply(..., max_mutations=...)`,
  `run_autopilot_cycle(..., max_mutations=...)`.
- `Settings.chainpilot_max_mutations`.

## Behavior
- Nominal: N>cap mutation edits → only the first `max_mutations` land; survivors
  recur next cycle. Cold-starts unaffected.
- `max_mutations=0` → no chain-mutating edit lands (cold-starts may still).
- A cycle within the cap behaves exactly as before.

## Architecture Impact
- Amends `chain_plan.py` (SP-CHAINPILOT-PLAN), `chain_loop.py`
  (SP-CHAINPILOT-LOOP), `settings.py`, `cli/main.py`; owns no new file. No new
  import edge (a parameter + a constant). `arch check` unchanged.

## Acceptance Criteria
- [ ] AC1: `plan_chain_rewrite` with 4 evict mutations and `max_mutations=2`
  applies exactly 2 (the slot keeps the other 2 + backstop); default also caps at 2.
- [ ] AC2: The cap threads through `plan_and_apply` (`max_mutations=0` blocks an
  eviction that recent faults would otherwise apply).
- [ ] AC3: `chainpilot_max_mutations` is a `FEROVA_*` flag defaulting to 2 (alias
  tests stay green).
- [ ] AC4: ruff + format + no-inline + `arch check` pass; full `pytest tests/unit`
  green.
- [ ] AC5: a negative cap fails CLOSED — `max_mutations=-1` applies 0 edits (the
  Field also rejects negative `FEROVA_CHAINPILOT_MAX_MUTATIONS`).
- [ ] AC6: a no-op edit (model absent from the chains) does not consume the cap —
  an absent-model evict next to an in-chain evict, `max_mutations=1`, applies the
  in-chain one. The runtime `chains.env.bak` is gitignored, not tracked.

## Open Questions
- The default (2) is conservative; tune via the flag once the corrected
  attribution (Fix-2/3) lands and shadow runs show realistic volumes.
