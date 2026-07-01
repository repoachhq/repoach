---
id: SP-CHAINPILOT-PLAN
title: Orchestrate the shadow chains.env rewrite (mutations + cold-start trials)
version: 0.1
status: draft
author: agent
created: 2026-06-26
updated: 2026-06-26

owns:
  code: src/ferova/review/chain_plan.py
  resources: N/A

depends_on:
  - SP-CHAINPILOT-CHAIN-REWRITE      # the mechanical rewriter + ChainEdit (3d-1a)
  - SP-CHAINPILOT-PLACE              # the placement classifier + profiles (3d-1b)
  - SP-CHAINPILOT-DECISION          # PlannedMutation / MutationKind (3b)
  - SP-CHAINPILOT-BENCHMARK-INGEST  # the benchmark prior (1c/3d-0)
  - SP-CHAINPILOT-EQUIVALENCES      # name↔canonical↔id resolver (1d)
  - SP-CHAINPILOT-MATRIX            # the live provider×model matrix (1b)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-APPLY-WRITE 3d-2 + SP-CHAINPILOT-LOOP 3e)
constraints: {}
---

# SP-CHAINPILOT-PLAN — turn decisions + the prior into a shadow rewrite

## Intent
Phase 3d-1c — the integration layer of 3d. Given the current `chains.env`, the
decision engine's mutations (3b), the benchmark prior (1c/3d-0), the equivalences
(1d), the live matrix (1b) and the healthy cells (2a), it produces the **shadow**
rewrite: the new file content plus the journalled mutations, **without touching
disk** (3d-2 writes, flag-gated; 3e wires the cadence). It is where cold-start
placement lives — the structural op 3b deferred because it needs chain + matrix +
tier context.

## Context
Two jobs only this layer can do:
- **Cold-start placement.** A benchmark model absent from every chain, whose tier
  the placement classifier (3d-1b) assigns and for which the live matrix offers a
  provider, is trialled by an `INSERT` into that tier — at most `per_tier` per
  cycle (default 1, a design parameter; the loop trials gradually).
- **Provider resolution.** Placement is in benchmark-canonical space; this layer
  resolves a canonical to a concrete `(provider, model_id)` cell via the
  equivalences + the live matrix, preferring a cell observed healthy.

Profiles are built **joined** (`profiles_from_ranking(ranking, equivalences=...)`,
amended in 3d-1b) so a model's quality entry and its differently-named coding
entry collapse onto one profile — without which the CODER gate would see
`quality_z = 0` for a coding-only fragment. The equivalence seed covers the open
candidates + the Mistral chain heads (the 2 entries this slice adds to 1d);
closed models whose fragments do not join still cannot be cold-started — they have
no cell in our matrix, so provider resolution drops them — which keeps the
cold-start set to genuinely reachable models.

## Goals
- G1: A new pure leaf `src/ferova/review/chain_plan.py` (no disk/DB/network;
  the matrix, healthy cells and mutations are injected — 3e gathers them).
- G2: `in_chain_canonicals(content, equivalences)` — the canonical (or raw id) of
  every model in the four chains.
- G3: `resolve_provider_cell(canonical, equivalences, matrix, healthy_cells)` — a
  matrix cell for the canonical: the cell must both map to the canonical AND
  carry the canonical's compact slug (the specificity guard against an unanchored
  `id_pattern` matching a sibling variant — `…-air` / `-flash` / non-`speciale`);
  among survivors a healthy cell wins, then the shortest id, else `None`.
- G4: `select_cold_starts(...)` — absent + resolvable candidates, the strongest
  fit per tier (up to `per_tier`), as `INSERT` edits + `COLD_START` journal
  mutations (a distinct `MutationKind` this slice adds to 3b, so the audit log
  never conflates a cold-start with a quality `promote`; `mutation_to_edit`
  returns `None` for it so it is never re-applied as a wrong structural edit).
  Only cold-starts whose `INSERT` actually lands (in `rewrite.applied`) are
  journaled.
- G5: `mutation_to_edit(mutation)` — map a `PlannedMutation` to a `ChainEdit`
  (`advise` → `None`).
- G6: `plan_chain_rewrite(content, mutations, *, ranking, equivalences, matrix,
  healthy_cells, per_tier_cold_starts) -> ChainRewritePlan(rewrite, cold_starts)`
  — compose placement + cold-start + mutation mapping through `rewrite_chains`.
- G7: Add the 2 Mistral chain-head equivalences to 1d; amend
  `profiles_from_ranking` with the optional `equivalences` join (3d-1b).

## Non-Goals
- NG1: Does NOT write `chains.env` (3d-2) nor read the DB / network / call the
  decision engine — all live inputs are injected.
- NG2: Does NOT compute faults/scoreboard/mutations (3a/2d/3b) nor sweep the
  matrix/health (2a) — 3e gathers and passes them.
- NG3: Does NOT journal — returns the cold-start mutations; 3e records them via
  the 3c audit log (`applied=False` until 3d-2 writes).

## Assumptions
- A1: `rewrite_chains` enforces all structural safety (backstop, never-empty,
  one-step, dedup, insert-above-backstop) — this layer only chooses edits.
- A2: A cold-start candidate is any joined profile absent from the chains with a
  resolvable provider cell; the benchmark prior already gates "is it a known
  model" (only ranked models have profiles).
- A3: `per_tier = 1` keeps the trial rate conservative; raising it is a tuning
  knob, not a code change.

## Interface
New (all in `chain_plan.py`): `ChainRewritePlan`, `in_chain_canonicals`,
`resolve_provider_cell`, `select_cold_starts`, `mutation_to_edit`,
`plan_chain_rewrite`, `DEFAULT_COLD_START_PER_TIER`. Amends (3d-1b)
`profiles_from_ranking(..., *, equivalences=None)` and (1d)
`benchmark_equivalences.json` (+2 Mistral entries).

## Behavior

### Nominal
- An absent open candidate (e.g. GLM-5.2) with a matrix cell → one `INSERT` into
  its placed tier + a `COLD_START` cold-start journal mutation.
- A `model_fault` mutation → one `evict_model` edit; `advise` → no edit.
- The four `MODEL_*` slots are rewritten only where an edit lands; comments stay
  verbatim (delegated to `rewrite_chains`).

### Edge cases
- A candidate already in a chain (by canonical) → not cold-started.
- A candidate with no matching matrix cell → not cold-started (unreachable).
- More than `per_tier` absent candidates for a tier → only the strongest fit(s).
- No mutations and no resolvable cold-starts → `new_content == content`.

### Failure scenarios
- Pure in-memory given its inputs; only `Chain.parse` on a malformed slot raises
  (a broken `chains.env`, surfaced upstream).

## Architecture Impact
- New pure leaf in `review/` importing `ChainEdit`/`rewrite_chains` (3d-1a),
  `place_candidates`/`profiles_from_ranking` (3d-1b), `PlannedMutation` (3b) from
  `review/`, and `ProviderModelMatrix`/`BenchmarkRanking`/`EquivalenceTable` from
  `llm_proxy` (the safe `review → llm_proxy` direction) — all governed edges
  declared. AMENDS `chain_placement.py` (3d-1b) + `benchmark_equivalences.json`
  (1d). No cycle.
- Nobody imports it yet (3d-2/3e will), so per
  [[unwired-invariant-breaks-next-slice]] no "nothing imports me" assertion is
  pinned and the FULL unit suite is run.

## Acceptance Criteria
- [ ] AC1: `in_chain_canonicals` returns each chain model's canonical (raw id
  when unmapped) across all four slots.
- [ ] AC2: `resolve_provider_cell` prefers a healthy cell, falls back to a present
  cell, returns `None` when the canonical has no matrix cell, and picks the
  specific model over a sibling variant (the `-speciale` over the non-`speciale`).
- [ ] AC3: `mutation_to_edit` maps evict/drop/demote/promote 1:1 and returns
  `None` for `advise`.
- [ ] AC4: `select_cold_starts` cold-starts only absent + resolvable candidates,
  at most `per_tier` per tier, emitting matched `INSERT` edits + `COLD_START`
  journal mutations; a cold-start whose `INSERT` is refused is not journaled.
- [ ] AC5: `plan_chain_rewrite` inserts a resolvable absent candidate into its
  placed tier and applies the mapped mutations; with no mutations and no
  resolvable cold-start, `new_content == content`.
- [ ] AC6: `profiles_from_ranking(..., equivalences=...)` collapses fragments
  under their canonical; without it, the standalone per-name behaviour is
  unchanged.
- [ ] AC7: ruff + format + no-inline + `arch check` pass; mypy-strict clean on the
  module; full `pytest tests/unit` green; the module does no I/O.

## Open Questions
- The cold-start rate (`per_tier`, default 1) and the within-tier priority
  (tier-fit strength) are conservative defaults — confirm with the operator if a
  different cold-start aggressiveness is wanted before 3e turns the loop on.
