---
id: SP-CHAINPILOT-PLACE
title: Tier placement classifier — semantic anchors over quality/speed/price/coding
version: 0.1
status: draft
author: agent
created: 2026-06-26
updated: 2026-06-26

owns:
  code: src/repoach/review/chain_placement.py
  resources: N/A

depends_on:
  - SP-CHAINPILOT-BENCHMARK-INGEST   # the benchmark prior it reads (1c + 3d-0 speed/price metrics)
  - SP-CHAINPILOT-EQUIVALENCES        # the optional name↔canonical join in profiles_from_ranking (added 3d-1c)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-PLAN-REWRITE-ORCHESTRATE, 3d-1c)
constraints: {}
---

# SP-CHAINPILOT-PLACE — which tier a cold-start model belongs in

## Intent
Phase 3d-1b — the *brain* of cold-start placement. A pure function that, given
the benchmark prior, decides which capability tier (`opus` / `sonnet` / `haiku`
/ `coder`) a model belongs in, from its `(quality, speed, price, coding)`
profile. It is the classifier 3d-1c calls when it needs to insert a never-seen
model into a chain; it holds the *policy* (the tier semantics), 3d-1a holds the
mechanics, 3d-1c the orchestration.

## Context
The operator's decision (2026-06-26): the four tiers are told apart by **explicit
semantic anchors per tier**, not by proximity to current chain members (rejected
— OPUS and SONNET share the head `mistral-medium-3.5`, so the members carry no
OPUS/SONNET signal). An anchor is a deliberate **design parameter** (like 3b's
`sigma`), documented as such — not a measurement masquerading as data.

Three real axes plus a coding axis, each z-scored over the benchmark population
(operator's call: **z-score**, more robust than min-max to the very different
dispersions of an intelligence rank vs tokens/sec vs $/Mtok):

- `quality` = `intelligence_index` (higher better),
- `speed` = `output_speed` tok/s (higher better),
- `price` = `price_blended` $/Mtok (lower better — encoded by the anchor sign,
  not by inverting the value),
- `coding` = the best z across the coding metrics (`coding_index`,
  `livecodebench_score`, `arena_elo_coding`); `0` when a model has no coding
  entry (so a model is only pulled toward CODER on positive coding evidence).

An anchor is a **priority DIRECTION**, not a point. A fixed anchor *point* with
Euclidean nearest-anchor was the first cut and the adversarial review proved it
fails the semantics: a zeroed coordinate means "prefer the population mean" not
"indifferent", and a fourth coding axis becomes a catch-all basin (on the real
snapshot 14/41 models, incl. 8 with no coding entry, drained into CODER). So each
**general** tier is a direction and a model is placed at the tier its z-vector
most strongly **aligns** with (largest projection onto the unit direction):

| Tier | quality | speed | price | meaning |
|---|---|---|---|---|
| OPUS | +1 | 0 | 0 | maximise quality, indifferent to speed/price |
| SONNET | +1 | +1 | −1 | good quality AND fast AND cheap |
| HAIKU | 0 | +1 | −1 | fastest + cheapest, quality irrelevant |

CODER is a **gate**, not a direction: a model goes to CODER only on positive
coding evidence that dominates its general standing (`coding_z > 0` and
`coding_z > quality_z` — a coding specialist), so a model with no coding entry is
never pulled to CODER. A top-quality slow/expensive model aligns with OPUS; a
good + fast + cheap model with SONNET; a fast + cheap, quality-secondary model
with HAIKU.

A general tier wins only on **positive** alignment (a positive projection), so a
below-average model is never dumped into a tier merely by being "least negative"
— the argmax failure mode that would otherwise make OPUS a slow/expensive basin
and HAIKU a data-sparse one. OPUS additionally requires positive quality
(`quality_z > 0`): the costly premium chain must never catch a mediocre model by
accident. With no positive alignment, or on a tie, the model defaults to SONNET
(the balanced workhorse, the safe cold-start default). The directions are
documented design parameters in `TIER_DIRECTIONS`.

## Goals
- G1: A new pure leaf `src/ferova/review/chain_placement.py` (no I/O, no DB,
  no network).
- G2: `CandidateProfile(model_name, quality, speed, price, coding)` — raw axis
  values (`None`/empty when absent) — and `Placement(model_name, tier, scores,
  coding_z)` (the chosen tier, the per-general-tier alignment projections, and
  the coding z, for transparency / journaling).
- G3: `profiles_from_ranking(ranking) -> tuple[CandidateProfile, ...]` — one
  profile per benchmark model, harvesting its quality / speed / price scores and
  its coding scores per coding metric.
- G4: `place_candidates(profiles) -> tuple[Placement, ...]` — z-score each axis
  over the supplied population (a missing value or a zero-variance axis → `0`),
  compute the coding z (max over coding metrics); a coding specialist
  (`coding_z > 0` and `coding_z > quality_z`) goes to CODER, else the model is
  placed at the general tier it aligns with most **positively** (largest positive
  projection of its `(quality, speed, price)` z-vector); OPUS also requires
  `quality_z > 0`; with no positive alignment, or on a tie, → SONNET.
- G5: `TIER_DIRECTIONS` exposed as a module constant (the design parameters),
  with the axis order documented; CODER's gate is documented likewise.

## Non-Goals
- NG1: Does NOT read the chains, the live matrix, the DB or the network — it
  classifies a profile; 3d-1c gathers candidates, resolves providers and writes.
- NG2: Does NOT identify *which* models are cold-start (absent from the chains) —
  that join (via the equivalences) is 3d-1c. **3d-1c must collapse the prior's
  per-source name fragments (via the equivalences) BEFORE building profiles**, so
  a model's coding score and its quality score sit on one profile — otherwise the
  CODER gate sees `quality_z = 0` for a coding-only name fragment and degenerates
  to `coding_z > 0` (a documented best-effort limit of this pure leaf, A3).
- NG3: Does NOT map `PlannedMutation` → `ChainEdit` nor call `rewrite_chains` —
  that orchestration is 3d-1c.
- NG4: Does NOT fuse the prior with live performance — placement is a cold-start
  prior only; the live loop (3b demote/promote) refines afterwards.

## Assumptions
- A1: A model's tier should not depend on which subset is placed alongside it, so
  z-scoring is over the full population passed in (3d-1c passes the full
  benchmark population, not just the cold-start subset).
- A2: `intelligence_index` is the single quality axis (most populated);
  `arena_elo_overall` is intentionally not mixed in (different scale, would
  double-count quality).
- A3: Coding benchmark coverage is sparse / top-truncated, so the coding gate is
  best-effort — a model with no coding entry is never placed in CODER (the gate
  requires `coding_z > 0`), and CODER wins only on positive coding evidence that
  dominates general quality. Reliable CODER cold-start awaits richer coding data;
  the mechanism is in place. (NB the prior's per-source names are not yet joined
  — NG2 — so a coding-only name fragment can still surface a positive coding z;
  3d-1c's equivalence join collapses those.)

## Interface
New (all in `chain_placement.py`):
- `TIER_DIRECTIONS: dict[str, tuple[float, float, float]]`.
- `@dataclass(frozen=True) class CandidateProfile`.
- `@dataclass(frozen=True) class Placement`.
- `def profiles_from_ranking(ranking) -> tuple[CandidateProfile, ...]`.
- `def place_candidates(profiles) -> tuple[Placement, ...]`.

## Behavior

### Nominal
- A high-quality (`quality_z > 0`), slow, expensive model (an Opus-class) → `opus`.
- A high-quality, fast, cheap model → `sonnet`.
- A fast, cheap, moderate-quality model (a Flash-class) → `haiku`.
- A model with a strong coding z and modest general quality → `coder`.
- A wholly average profile → `sonnet`.

### Edge cases
- A zero-variance axis (all equal, or one value) → every z on it is `0`
  (neutral), so it does not skew placement.
- A profile missing an axis → `0` on that axis (treated as average).
- A model with no coding entry → coding z `0` → the CODER gate (`coding_z > 0`)
  never fires, so it is never placed in CODER.
- A below-average / slow + expensive model never lands in OPUS (the `quality_z > 0`
  floor); a model with no positive alignment to any general tier → SONNET (so a
  data-sparse weak model defaults to SONNET, not the cheapest tier by accident).
- A tie among the general directions → SONNET.
- Empty input → empty output.

### Failure scenarios
- Pure in-memory; no raise path beyond a malformed ranking (the loader's
  concern, fail-loud upstream).

## Architecture Impact
- New pure leaf in `review/` importing `BenchmarkRanking` from `llm_proxy`
  (the safe `review → llm_proxy` direction; the one governed edge declared). No
  cycle.
- Nobody imports it yet (3d-1c will), so per
  [[unwired-invariant-breaks-next-slice]] no "nothing imports me" assertion is
  pinned and the FULL unit suite is run.

## Acceptance Criteria
- [ ] AC1: `profiles_from_ranking` on the shipped snapshot yields one profile per
  model, with quality/speed/price populated where the metric exists and the
  coding map carrying any coding-metric scores.
- [ ] AC2: `place_candidates` assigns by alignment (and the CODER gate); the four
  nominal cases land in their expected tiers on a representative population.
- [ ] AC3: A zero-variance or missing axis contributes `0` (no crash, no skew).
- [ ] AC4: NO model with an empty coding map is ever placed in `coder` — verified
  on the full shipped snapshot — while a synthetic strong-coding profile is; the
  CODER gate requires `coding_z > 0` and `coding_z > quality_z`.
- [ ] AC5: A tie among the general directions, or no positive alignment to any,
  resolves to `sonnet`; empty input → empty.
- [ ] AC6: A below-average / slow + expensive model never lands in `opus` (the
  `quality_z > 0` floor), and a data-sparse weak model defaults to `sonnet` not
  `haiku` — both verified; `TIER_DIRECTIONS` is the single source of the
  direction parameters.
- [ ] AC7: ruff + format + no-inline + `arch check` pass; mypy-strict clean on
  the module; full `pytest tests/unit` green; the module is pure (no I/O).

## Open Questions
- None for the classifier. (Cold-start identification, provider resolution via
  the live matrix, the `PlannedMutation` → `ChainEdit` mapping and the 3c journal
  are 3d-1c; the Mistral chain-head equivalences land there too.)
