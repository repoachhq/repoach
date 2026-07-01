---
id: SP-CODER-TIER-RETIRE-AGENT
title: Retire the CODER capability tier (agent side) — coding agents route to SONNET
version: 0.1
status: draft
author: agent
created: 2026-06-28
updated: 2026-06-28

owns:
  code: []
  resources: []

depends_on: []
provides_to: []
constraints: {}
---

# SP-CODER-TIER-RETIRE-AGENT — drop the CODER capability tier on the agent side

## Intent
First of three ordered sub-specs that retire the **CODER capability tier**. The tier was
justified only by a code-specialist model (`qwen3-coder-480b`); that model is EOL on NIM
(HTTP 410), so `MODEL_CODER` now lists the same general models as the other tiers and the
tier is redundant. There are fundamentally three capability classes — `haiku` / `sonnet` /
`opus` (mirroring the model sizes); `coder` is a fourth, artificial tier.

This sub-spec removes the tier on the **agent side** and routes the three coding agents
(Planner, Coder, Developer) to **SONNET** (operator's call — sonnet quality is the right
level for the loop). After this change no agent emits the `coder` alias, so the proxy's
coder slot becomes dead-but-harmless until the proxy-side and chains sub-specs remove it.

This is a cross-cutting refactor: it **owns no code** (it edits files owned by the llm /
agent-engine / review components); `owns` is empty by design.

## Context
The `BotRole.CODER` review-bot role, the `Coder` review agent, and the `coder_loop` /
`coder_findings` / `MAX_CODER_ROUNDS` machinery are a DIFFERENT concept (the findings Coder
agent) and are **out of scope** — only the *capability tier* is removed. The two later
sub-specs depend on this one: **SP-CODER-TIER-RETIRE-PROXY** (removes `Tier.CODER`,
`model_coder`) and **SP-CODER-TIER-RETIRE-CHAINS** (removes `MODEL_CODER` + the chainpilot
placement gate). The order keeps every intermediate state consistent.

## Goals
- G1: Remove `CapabilityTier.CODER` from `llm/capability.py` (enum member + the
  `CAPABILITY_TO_ALIAS` entry `claude-coder-ferova` + the docstrings that name a fourth
  / "code-specialist" tier).
- G2: Remove `PROXY_CODER_CHAIN` from `agent_engine/agent_loop.py` (the constant, its
  `__all__` export, the `"coder"` branch of `_capability_for_alias`, and docstrings).
- G3: Route the three coding agents to `PROXY_SONNET_CHAIN`: `review/reviewer.py`'s `Coder`
  and `Developer`, and `review/planner.py`'s `Planner` (drop the `PROXY_CODER_CHAIN`
  imports; the agents' `max_tokens` are unchanged).
- G4: Update the unit tests that reference the agent-side CODER tier so the suite is green
  (capability/alias mapping, `capability=CapabilityTier.CODER` dispatch, Coder/Developer
  construction).

## Non-Goals
- NG1: No change to the proxy `Tier.CODER` / `model_coder` / routing (SP-CODER-TIER-RETIRE-PROXY)
  or to `chains.env` `MODEL_CODER` / the chainpilot placement gate (SP-CODER-TIER-RETIRE-CHAINS).
  Those remain present but unused after this sub-spec.
- NG2: No change to the `Coder` review agent's identity — `BotRole.CODER`, `coder_loop`,
  `coder_findings`, `MAX_CODER_ROUNDS` all stay (they are the findings Coder, not the tier).
  Only the agent's `model_chain` pointer changes.

## Interface
- `llm.capability.CapabilityTier` loses its `CODER` member; `CAPABILITY_TO_ALIAS` loses the
  coder entry.
- `agent_engine.agent_loop` no longer exports `PROXY_CODER_CHAIN`; `_capability_for_alias`
  no longer classifies `"coder"`.

## Behavior
- Planner / Coder / Developer dispatch over the SONNET chain (alias `sonnet`), which the
  proxy walks exactly as before for sonnet — the only change is which `MODEL_*` chain backs
  the coding agents.
- An inbound alias containing `"coder"` no longer maps to a CODER tier on the agent side
  (none is produced); the proxy's still-present coder slot is simply never addressed.

## Architecture Impact
- Owns nothing. Edits files owned by the `llm` (capability), `agent-engine` (agent_loop),
  and `review` (reviewer/planner) components. No new import edges; `PROXY_CODER_CHAIN`
  usages are repointed to the existing `PROXY_SONNET_CHAIN`. `depends_on: []`.

## Acceptance Criteria
- [ ] AC1: `CapabilityTier` has no `CODER` member and `CAPABILITY_TO_ALIAS` has no
  `claude-coder-ferova`; `agent_loop` does not define or export `PROXY_CODER_CHAIN`,
  and `_capability_for_alias("…coder…")` no longer returns a CODER tier.
- [ ] AC2: `Planner`, `Coder`, and `Developer` each have `model_chain == PROXY_SONNET_CHAIN`.
- [ ] AC3: The `Coder` review agent still exists with `role == BotRole.CODER`; `coder_loop`,
  `coder_findings`, and `MAX_CODER_ROUNDS` are untouched.
- [ ] AC4: ruff + format + no-inline + no-silent-except + `arch check` + full `pytest
  tests/unit` green under 3.11 and 3.13 (updated capability/agent-loop/review tests included).

## Open Questions
- None. The proxy's residual `Tier.CODER` / `model_coder` and `chains.env`'s `MODEL_CODER`
  are intentionally left for the next two sub-specs; they are inert after this change.
