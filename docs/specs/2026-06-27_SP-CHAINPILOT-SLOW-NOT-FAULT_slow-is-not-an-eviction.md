---
id: SP-CHAINPILOT-SLOW-NOT-FAULT
title: A slow probe is not a fault (attribution never evicts on latency)
version: 0.1
status: draft
author: agent
created: 2026-06-27
updated: 2026-06-27

owns:
  code: N/A
  resources: N/A

depends_on:
  - SP-CHAINPILOT-ATTRIBUTION   # amends the classifier (3a)

provides_to: []
constraints: {}
---

# SP-CHAINPILOT-SLOW-NOT-FAULT — latency is a quality signal, not grounds to evict

## Intent
Fix-2 of four after the 2026-06-27 armed-autopilot incident. The classifier
counted `slow` probes against cell health (`n_slow` sat in the denominator but
not the numerator of `cell_is_healthy`) and listed `slow` as an attributable
failure mode. NIM is volatile (a head intermittently returns `slow`/`ReadTimeout`
— confirmed live the same day: `coder deepseek-v4-pro` flips ok↔slow↔error within
minutes). So a working-but-occasionally-slow head was driven unhealthy over the
window and **evicted**. A slow response still returned content: the model works.
Latency belongs to the quality lane (demote / deprioritise), never to eviction.

## Context
`cell_is_healthy` is the single shared definition of cell health (attribution 3a
+ the loop's healthy-cell selection 3e). Two changes:

1. **`slow` counts as healthy**: the bar is now
   `(n_ok + n_slow) / n_samples >= ok_fraction`. A cell that responds with
   content (fast or slow) most of the time is healthy and is never evicted.
2. **`slow` is removed from `_dominant_failure`**: an unhealthy cell is attributed
   only to `starved` (our output budget → OUR_FAULT) or `dead` (errored / empty
   with no reasoning → PROVIDER/MODEL fault). Latency can no longer be the
   attributed cause of an eviction even when a cell is unhealthy for other reasons.

This alone neutralises the incident's NIM evictions: `deepseek-v4-pro` on NIM
(1 ok / 2 slow / 1 error → (1+2)/4 = 0.75) and `mistral-medium-3.5`
(slow-dominated) are both healthy again.

Scope note: this protects `slow` (content-returning) probes only. A hard
`ReadTimeout`/error is a `dead` probe, so a head that times out for *most* of the
window is still unhealthy → evicted (model-wide) or dropped (per-provider, once
Fix-3 lands) — which is correct, it is genuinely unusable. The boundary uses `>=`
(pre-existing): a cell returning content exactly `ok_fraction` of the time counts
as healthy. Fix-2 only widens the healthy numerator to `ok + slow`; it does not
claim to shield timeout-dominated cells.

## Goals
- G1: `cell_is_healthy` counts `ok + slow` toward the bar.
- G2: `_dominant_failure` ranks only `starved` vs `dead` (slow excluded).
- G3: Docstrings (module, `cell_is_healthy`, `DEFAULT_OK_FRACTION`,
  `_dominant_failure`) state the new semantics.

## Non-Goals
- NG1: Does NOT add a latency-driven demote (a slow head staying at the chain head
  is a separate quality concern — see Open Questions / a later fix).
- NG2: Does NOT touch the cross-provider scope (Fix-3) nor cold-start (Fix-4).

## Assumptions
- A1: A `slow` probe genuinely returned usable content (the probe layer already
  distinguishes slow from empty/error), so treating it as healthy is correct.

## Interface
- `attribution.cell_is_healthy`, `attribution._dominant_failure` (behaviour only;
  signatures unchanged). Shared with `chain_loop.select_healthy_cells` (3e).

## Behavior
- `slow` everywhere → HEALTHY (was MODEL_FAULT).
- `ok`/`slow` mix with a minority of `dead` that still clears the bar → HEALTHY.
- A cell unhealthy via `dead` with `slow` present → attributed to `dead`
  (MODEL/PROVIDER fault), never `slow`.

## Architecture Impact
- Amends `attribution.py` (SP-CHAINPILOT-ATTRIBUTION). No new import edge; the
  shared `cell_is_healthy` change also relaxes the loop's healthy-cell selection
  (a slow cell is now eligible — acceptable: a working fallback), keeping the two
  definitions aligned by construction.

## Acceptance Criteria
- [ ] AC1: slow-everywhere is HEALTHY; the prior MODEL_FAULT/PROVIDER_FAULT
  slow tests are updated to assert health.
- [ ] AC2: an unhealthy-via-dead cell with slow present is attributed to `dead`
  (its reason never says `slow`).
- [ ] AC3: ruff + format + no-inline + mypy(attribution) + full `pytest tests/unit`
  green.

## Open Questions
- A persistently-slow head still leads its chain (works, but slow). A latency
  demote (move slow heads down a rank without evicting) is the natural follow-up
  — deferred so this fix stays a single, reviewable behaviour change.
