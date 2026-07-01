---
id: SP-CHAINPILOT-SPEED-PRICE-INGEST
title: Benchmark prior — add the speed + price dimension
version: 0.1
status: draft
author: agent
created: 2026-06-26
updated: 2026-06-26

owns:
  code: N/A
  resources: N/A

depends_on:
  - SP-CHAINPILOT-BENCHMARK-INGEST   # the snapshot + parser this extends (1c)

provides_to: []                  # AUTO-maintained (consumed by SP-CHAINPILOT-PLAN-REWRITE, 3d-1)
constraints: {}
---

# SP-CHAINPILOT-SPEED-PRICE-INGEST — the missing operational axis

## Intent
Phase 3d-0 of the Chain Autopilot arc. The benchmark prior (1c) carries only
**quality** metrics (intelligence / coding / arena Elo). The 4-tier cold-start
placement decided for 3d (operator, 2026-06-26) needs to tell OPUS, SONNET and
HAIKU apart — and those tiers differ by **speed and cost**, not by a lower
quality rank. This slice ingests the speed (tokens/sec) and price (USD per 1M
tokens) figures Artificial Analysis already publishes for the same models,
turning the prior into a `(quality, speed, price)` profile. Pure data + a tiny
schema relaxation; no behavior wired here.

## Context
The capture (2026-06-26) confirmed the missing axis is real and discriminating:
our three live chain heads sit at very different points —

| Model (chain head) | Output speed (tok/s) | Blended price ($/Mtok) |
|---|---|---|
| Mistral Medium 3.5 (OPUS+SONNET head) | 140.7 | 1.16 |
| Mistral Small 4 (Reasoning) (HAIKU head) | 163.5 | 0.20 |
| DeepSeek V4 Pro (Max) (CODER head) | 78.7 | 0.18 |

and the frontier closed models cluster slow + expensive (Opus 4.8 (max): 61 t/s,
$3.85; Sonnet 4.6 (max): 54 t/s, $2.31) while the Flash / open tier clusters
fast + cheap (Gemini 3.5 Flash: 192 t/s, $1.31; Qwen3.7 Max: 207.8 t/s, $1.43).
That spread is exactly the gradient a 4-tier split needs and the quality-only
prior could not express.

**Schema relaxation.** A leaderboard *position* (`rank`) is meaningful for a
ranking metric but not for a raw per-model attribute like speed or price, where
the captured value (the `score`) is the datum and no global rank was read. So
`BenchmarkEntry.rank` becomes optional (`int | None`, default `None`); existing
ranking entries keep their integer ranks, attribute entries omit it. The single
`rank` consumer (`perf_aggregate._resolve_prior`) is guarded to skip rank-less
entries, so a prior is still only ever resolved on a true ranking metric.

**Provenance & honesty (Principle 3, 1c discipline).** Every figure is read
verbatim from Artificial Analysis (per-model pages for the chain heads + open
models, the cross-verified leaderboard for the frontier closed models). No
figure is synthesised or interpolated. Six lower-priority candidates
(MiniMax-M3, Kimi K2.6, MiMo-V2.5-Pro, Muse Spark, Opus 4.7 Non-reasoning,
DeepSeek V3.2 Speciale) had no reliably readable speed/price (JS-rendered charts,
browser extension unavailable this session) and are **omitted, not guessed** —
to be backfilled when a rendered-page capture is possible.

## Goals
- G1: `BenchmarkEntry.rank` is `int | None` (default `None`); the parser accepts
  entries that omit `rank` and existing ranking entries are unchanged.
- G2: `benchmark_prior.json` gains two source blocks — `artificial_analysis_speed`
  (metric `output_speed`, tok/s, higher better) and `artificial_analysis_price`
  (metric `price_blended`, USD/1M tokens, **lower** better) — each with full
  provenance (url, capture date, scale).
- G3: Real captured `output_speed` entries (16) and `price_blended` entries (18),
  including all three chain heads, using each model's name **verbatim** and
  matching the existing intelligence-index `model_name` strings where the model
  already appears (so a downstream join is by model name).
- G4: `_resolve_prior` skips entries with `rank is None`, so a prior rank is only
  ever taken from a ranking metric.

## Non-Goals
- NG1: Does NOT wire speed/price into any decision — the 4-tier placement
  classifier is 3d-1.
- NG2: Does NOT add the chain-head → AA-name equivalences needed to join the
  heads by provider id (a 1d/3d-1 concern; flagged below).
- NG3: Does NOT backfill the six omitted candidates, nor input/output price
  splits (blended is sufficient for the tier gradient).
- NG4: Does NOT change how `prior_metric` is chosen, nor add per-tier prior
  selection.

## Assumptions
- A1: A blended price and a median output speed are stable enough model-level
  priors (Principle 3 — the live probe latency overrides for in-chain models).
- A2: `score` remains required; only `rank` is relaxed — so a malformed entry
  missing `score` still fails loud (1c contract preserved).
- A3: AA model names are consistent across its own metric tables, so the same
  `model_name` string joins a model's quality, speed and price entries.

## Interface
- `BenchmarkEntry.rank: int | None = None` (was `int`). No new symbols, no new
  files; the existing query helpers (`by_metric`, `entries_for_model`) already
  serve the new metrics.

## Behavior

### Nominal
- `ranking.by_metric("output_speed")` returns the speed entries; each has a
  positive `score` and `rank is None`.
- `ranking.by_metric("price_blended")` returns the price entries (lower better).
- `ranking.entries_for_model("Mistral Medium 3.5")` returns its speed and price
  entries (the head is now profiled on the operational axis).
- A prior on `intelligence_index` still resolves exactly as before.

### Edge cases
- A `prior_metric="output_speed"` resolves to `prior_rank=None` (rank-less
  entries are skipped) rather than crashing.
- Models without a readable figure simply have no entry for that metric.

### Failure scenarios
- A malformed snapshot (missing `score`, unknown field) still raises
  `ValidationError` at load — the resource is trusted, fail loud.

## Architecture Impact
- AMENDS `src/ferova/llm_proxy/providers/benchmark_prior.py` and its
  `benchmark_prior.json` (owned by SP-CHAINPILOT-BENCHMARK-INGEST) and the guard
  in `src/ferova/review/perf_aggregate.py` (owned by
  SP-CHAINPILOT-PERF-AGGREGATE). Owns no new file. No new import edge (additive
  field + data + a one-line guard), so `arch check` is unchanged.
- DOWNSTREAM FLAG for 3d-1: the chain heads are Mistral models absent from the
  1d equivalence seed; joining `nvidia_nim/mistralai/mistral-medium-3.5-128b` →
  `Mistral Medium 3.5` (and Small 4) needs equivalence entries. 3d-1 (or a small
  1d amendment) must add them before the placement classifier can read the heads.

## Acceptance Criteria
- [ ] AC1: `BenchmarkEntry(rank omitted)` parses with `rank is None`; a ranking
  entry with an integer `rank` is unchanged.
- [ ] AC2: A payload missing `score` still raises `ValidationError`.
- [ ] AC3: The shipped snapshot loads; `by_metric("output_speed")` and
  `by_metric("price_blended")` are both non-empty; every such entry has
  `rank is None` and a positive `score`; both new sources are declared.
- [ ] AC4: All three chain heads (Mistral Medium 3.5, Mistral Small 4
  (Reasoning), DeepSeek V4 Pro (Max)) have both a speed and a price entry.
- [ ] AC5: `aggregate_model_performance(..., prior_metric="output_speed")`
  yields `prior_rank is None` (guard holds); a prior on a ranking metric is
  unaffected.
- [ ] AC6: ruff + format + no-inline-comments pass; full `pytest tests/unit`
  green; `arch check` passes.

## Open Questions
- None for this slice. (The placement rule that consumes this — nearest
  tier-exemplar fit on quality×speed×price — is 3d-1's design.)
