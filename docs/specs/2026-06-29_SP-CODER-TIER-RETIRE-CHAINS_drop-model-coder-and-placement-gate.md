---
id: SP-CODER-TIER-RETIRE-CHAINS
title: Retire the CODER capability tier (chains side) — drop MODEL_CODER and the chainpilot coder gate
version: 0.1
status: draft
author: agent
created: 2026-06-29
updated: 2026-06-29

owns:
  code: []
  resources: []

depends_on: []
provides_to: []
constraints: {}
---

# SP-CODER-TIER-RETIRE-CHAINS — drop the CODER tier from chains.env and chainpilot

## Intent
Third and final sub-spec retiring the **CODER capability tier**.
[[SP-CODER-TIER-RETIRE-AGENT]] (#469) stopped the agents emitting the `coder` alias and
[[SP-CODER-TIER-RETIRE-PROXY]] (#470) removed `Tier.CODER` / `model_coder` from the proxy.
This sub-spec removes the last two pieces: the `MODEL_CODER` line in `chains.env` and the
chainpilot machinery that knows a `coder` tier — the cold-start **placement gate** and the
chain-mutation slot/tier tables.

After this change the system knows exactly three capability tiers — `opus` / `sonnet` /
`haiku` — end to end: agents, proxy, chains.env, and chainpilot.

This is a cross-cutting refactor: it **owns no code** (it edits `chains.env` and files owned
by the `review` component); `owns` is empty by design.

## Context
**This change is atomic by necessity.** The chainpilot (`ferova autopilot`) rewrites
`chains.env` on a 6-hour timer; removing `MODEL_CODER` without removing the placement gate
(or vice-versa) would let the next armed cycle re-derive a `coder` chain. The `chains.env`
edit and the chainpilot coder removal therefore ship in one PR. (Operational note: the
operator disarms / re-arms the chainpilot timer around the deploy; this spec does not touch
the systemd units.)

The cold-start **placement gate** routed a model to `coder` on positive coding evidence that
dominated its general standing (`coding_z > 0` and `coding_z > quality_z`). The `coding_z`
axis (computed from `coding_index` / `livecodebench_score` / `arena_elo_coding`) existed
**only** to feed that gate, so it is removed with it — placement now classifies purely on the
`(quality, speed, price)` semantic directions.

The per-model coding-outcomes harvest (`review/coder_outcomes.py`,
[[SP-CHAINPILOT-CODER-OUTCOMES]]) is **orthogonal and untouched**: it aggregates the findings
Coder agent's real merge outcomes keyed by `pr_coder_responses.model_used` (a model string),
not by any placement tier, and keeps working for whichever tier serves Coder requests.

## Goals
- G1: Remove the `MODEL_CODER` block (comment + key) from `chains.env`. A residual value is
  already inert (the proxy ignores it since #470), but the canonical file should be honest.
- G2: Remove the coder placement gate from `review/chain_placement.py`: the `coding_z` gate
  condition in `place_candidates`, `_coding_z`, `_CODING_METRICS`, the `coding` field of
  `CandidateProfile` and its harvest in `profiles_from_ranking`, and the `coding_z` field of
  `Placement` (plus the docstrings that name a coder tier / gate).
- G3: Remove the coder slot/tier in the chain-mutation layer: `"MODEL_CODER": "coder"` from
  `chain_plan._SLOT_KEYS`, the `placement.tier == "coder"` branch of
  `chain_plan._cold_start_priority`, and `"coder"` from `chain_rewrite._TIERS`.
- G4: Remove the residual `"coder"` from the `capability` `Literal` in
  `llm_proxy/api/models/agent_v1.py` (the agent request/response schema).
- G5: Update the unit tests that assert the chains-side coder tier (chains single-source,
  placement gate, chain rewrite/plan fixtures) so the suite is green.

## Non-Goals
- NG1: No change to the findings Coder agent (`BotRole.CODER`, `coder_loop`, `coder_findings`,
  `MAX_CODER_ROUNDS`) or to `review/coder_outcomes.py` — they are orthogonal to the tier.
- NG2: No change to the chainpilot systemd units (`deploy/systemd/ferova-chainpilot.*`) —
  the operator manages the arm/disarm around deployment.

## Interface
- `chains.env` defines exactly `MODEL`, `MODEL_OPUS`, `MODEL_SONNET`, `MODEL_HAIKU`.
- `chain_placement.CandidateProfile` and `Placement` lose their coding fields;
  `place_candidates` never returns a `coder` tier.
- `chain_plan._SLOT_KEYS` and `chain_rewrite._TIERS` cover three tiers.

## Behavior
- The chainpilot cold-start placement classifies every candidate into `opus` / `sonnet` /
  `haiku` by the `(quality, speed, price)` semantic directions; a model that previously would
  have gated into `coder` now lands in the tier its general profile best aligns with.
- A chainpilot rewrite of `chains.env` only ever touches the three remaining slots.

## Architecture Impact
- Owns nothing. Edits `chains.env` (a resource) and files owned by the `review` component
  (`chain_placement`, `chain_plan`, `chain_rewrite`) plus the `llm_proxy` agent schema. No new
  import edges; an orphaned axis and a slot are removed. `depends_on: []`.

## Acceptance Criteria
- [ ] AC1: `chains.env` has no `MODEL_CODER` key; `chain_plan._SLOT_KEYS` and
  `chain_rewrite._TIERS` contain only `opus` / `sonnet` / `haiku`.
- [ ] AC2: `place_candidates` never assigns the `coder` tier; `CandidateProfile` and
  `Placement` carry no coding fields, and `chain_placement` defines no `_coding_z` /
  `_CODING_METRICS`.
- [ ] AC3: `agent_v1` `capability` is `Literal["opus", "sonnet", "haiku"]`.
- [ ] AC4: The findings Coder agent and `review/coder_outcomes.py` are unchanged.
- [ ] AC5: ruff + format + no-inline + no-silent-except + `arch check` + full `pytest
  tests/unit` green under 3.11 and 3.13 (updated placement / chain-plan / chain-rewrite /
  chains-single-source tests included).

## Open Questions
- None. With this sub-spec the CODER capability tier is gone end to end.
