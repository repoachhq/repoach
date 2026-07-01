---
id: SP-CODER-TIER-RETIRE-PROXY
title: Retire the CODER capability tier (proxy side) — drop Tier.CODER and model_coder
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

# SP-CODER-TIER-RETIRE-PROXY — drop the CODER tier on the proxy side

## Intent
Second of three ordered sub-specs that retire the **CODER capability tier**.
[[SP-CODER-TIER-RETIRE-AGENT]] already removed the tier on the agent side: no agent
emits the `coder` alias anymore, so the proxy's coder slot is dead-but-harmless. This
sub-spec removes the slot from the proxy: the `Tier.CODER` enum member, its
`classify_tier` substring branch, the `model_coder` setting, and the routing-table /
probe-seed / chain-health plumbing that reads it.

After this change the proxy knows only three capability tiers — `opus` / `sonnet` /
`haiku` — plus `DEFAULT`. `chains.env` still carries a `MODEL_CODER` line, which
`Settings` now silently ignores (`extra="ignore"`); the third sub-spec
(**SP-CODER-TIER-RETIRE-CHAINS**) removes that line and the chainpilot placement gate.

This is a cross-cutting refactor: it **owns no code** (it edits files owned by the
`llm_proxy` and `review` components); `owns` is empty by design.

## Context
Because `Settings.model_config` uses `extra="ignore"`, a residual `MODEL_CODER` in
`chains.env` / `.env` is harmless after the field is dropped — no removed-env-var
fail-fast is registered for it (that mechanism is reserved for renames like
`NIM_ENABLE_THINKING`). This keeps every intermediate state consistent: the proxy runs
green against the unchanged `chains.env` between this sub-spec and the chains one.

The chainpilot placement gate (`review/chain_placement.py`, `review/chain_plan.py`,
`review/chain_rewrite.py`) still references the `"coder"` tier *as a string* and
`coding_z`; it reads `chains.env` slots, **not** `settings.model_coder`, so it is
untouched here and removed in SP-CODER-TIER-RETIRE-CHAINS.

## Goals
- G1: Remove `Tier.CODER` from `llm_proxy/routing/tier.py` (the enum member and the
  `if "coder" in name_lower: return Tier.CODER` branch of `classify_tier`), and update
  the docstrings that name a `coder` substring match.
- G2: Remove the `model_coder` field from `llm_proxy/config/settings.py` and the
  docstrings that describe it; leave `extra="ignore"` so a residual `MODEL_CODER`
  is ignored rather than rejected.
- G3: Remove the `Tier.CODER: settings.model_coder` slot from
  `RoutingTable.from_settings` (`llm_proxy/routing/table.py`) and update its docstring.
- G4: Drop `"coder"` from `_TIERS` and the `"coder": settings.model_coder` entry in
  `review/chain_health.py` so the head-health probe no longer probes a coder head.
- G5: Update the residual `coder` mentions in `llm_proxy/routing/probe_seed.py`
  (docstring), `llm_proxy/__init__.py` (docstring), and
  `llm_proxy/config/env.example` (drop the `MODEL_CODER` mention).
- G6: Update the unit tests that assert the proxy-side coder tier so the suite is green.

## Non-Goals
- NG1: No change to `chains.env`'s `MODEL_CODER` line or the chainpilot placement gate
  (`chain_placement` / `chain_plan` / `chain_rewrite`) — that is
  SP-CODER-TIER-RETIRE-CHAINS. They stay present but inert after this sub-spec.
- NG2: No change to the `Coder` review agent (`BotRole.CODER`, `coder_loop`,
  `coder_findings`, `MAX_CODER_ROUNDS`) — that is the findings Coder, not the tier.
- NG3: No new removed-env-var fail-fast for `MODEL_CODER` (would break the intermediate
  state against the unchanged `chains.env`).

## Interface
- `llm_proxy.routing.tier.Tier` loses its `CODER` member; `classify_tier` never returns
  it and matches first-match-wins in the order `opus` → `haiku` → `sonnet`.
- `llm_proxy.config.settings.Settings` loses its `model_coder` field.
- `RoutingTable.from_settings` builds chains for `OPUS` / `SONNET` / `HAIKU` only
  (plus `DEFAULT`).

## Behavior
- An incoming model name containing `"coder"` no longer classifies to a CODER tier; it
  falls through the remaining substring matches and otherwise resolves to `DEFAULT`
  (the global `MODEL` chain). Since no agent emits a `coder` alias anymore, this path is
  never exercised in practice.
- `Settings` loads cleanly against a `chains.env` that still defines `MODEL_CODER`
  (the value is ignored).

## Architecture Impact
- Owns nothing. Edits files owned by the `llm_proxy` (routing/tier, config/settings,
  routing/table, routing/probe_seed) and `review` (chain_health) components. No new
  import edges; an existing slot read is removed. `depends_on: []`.

## Acceptance Criteria
- [ ] AC1: `Tier` has no `CODER` member and `classify_tier("…coder…")` no longer returns
  a CODER tier (it returns `DEFAULT` when no other substring matches).
- [ ] AC2: `Settings` has no `model_coder` field, and instantiating `Settings()` against
  an env that still defines `MODEL_CODER` succeeds (no validation error).
- [ ] AC3: `RoutingTable.from_settings` never builds a `Tier.CODER` chain;
  `review/chain_health.py` `_TIERS` is `("opus", "sonnet", "haiku")` and no longer reads
  `settings.model_coder`.
- [ ] AC4: ruff + format + no-inline + no-silent-except + `arch check` + full
  `pytest tests/unit` green under 3.11 and 3.13 (updated routing/settings/chain-health
  tests included).

## Open Questions
- None. `chains.env`'s `MODEL_CODER` and the chainpilot placement gate are intentionally
  left for SP-CODER-TIER-RETIRE-CHAINS; they are inert after this change.
