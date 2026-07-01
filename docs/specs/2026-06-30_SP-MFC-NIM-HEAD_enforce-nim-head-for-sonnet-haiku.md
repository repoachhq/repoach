---
id: SP-MFC-NIM-HEAD
title: Enforce a NIM head for the sonnet + haiku tiers in model-first expansion
version: 0.1
status: draft
author: Claude (design dialogue with operator)
created: 2026-06-30
updated: 2026-06-30

owns:
  code: []
  resources: []

depends_on: []
provides_to: []
constraints:
  nim_provider: nvidia_nim
  nim_head_tiers: [sonnet, haiku]
---

# SP-MFC-NIM-HEAD — enforce a NIM head for sonnet + haiku

## Intent

Make the model-first chain generator guarantee that the **sonnet** and **haiku**
tier chains are headed by a free NIM cell. Today the generator selects models by
capability only ([[SP-MFC-SELECT]]) and expands them NIM-first *within* each model
([[SP-MFC-EXPAND]]) — but the chain **head** is the first provider of the highest
*capability* model, so when that model is not NIM-served the head is a paid
OpenRouter cell. On the two highest-volume tiers that is a cost regression and it
breaks the architecture invariant "NIM stays the free head of sonnet + haiku"
(`docs/model_first_chains_architecture.md`, strategic decision A). OPUS is
deliberately exempt: it is the paid frontier tier by design.

## Context

This closes a design gap surfaced live on 2026-06-30: the first model-first
`regenerate-chains` output headed sonnet and haiku with
`open_router/qwen/qwen3.7-max` (paid). The gap was patched by hand in `chains.env`
(promoting the first NIM cell to the head), but a hand edit is overwritten by the
armed 6h chainpilot cadence (`regenerate-chains --apply`). The rule must live in
the generator so regeneration produces NIM-headed sonnet/haiku chains on its own.

The fix is intrinsic to provider expansion, so it edits the EXPAND-owned leaf
`src/ferova/llm_proxy/routing/chain_expand.py`. This is a behavioural refactor
of an existing component: it **owns no code** (`chain_expand.py` belongs to
[[SP-MFC-EXPAND]]); `owns` is empty by design. The change is internal to that
module — a private helper plus a defaulted parameter — so it introduces no new
cross-`owns` import edge and the edge-honesty gate stays green.

## Behaviour

`expand_tier` (and `expand_chains`) gain a `nim_head_tiers` parameter, defaulting
to `frozenset({"sonnet", "haiku"})`. For a tier in that set, after the per-model
NIM-first expansion has assembled the ordered `provider/model` entries (and before
the `claude_code/<tier>` tail is appended):

- If the head entry is already a NIM cell, the chain is unchanged.
- Otherwise the **first** NIM cell in the chain — the highest-capability NIM-served
  model, since the entries are already in capability-then-NIM-first order — is
  promoted to the head; the relative order of every other entry is preserved.
- If no NIM cell serves the tier at all, the capability head is kept and the gap is
  logged (`mfc_nim_head_unavailable`). This should not happen for sonnet/haiku
  (NIM fills both bands) but must degrade gracefully rather than crash.

A tier absent from `nim_head_tiers` (i.e. `opus`) is never reordered. The default
makes the policy active without any caller change, so `chain_generate` /
`chain_regen` and the live cadence pick it up with no rewiring.

This generalises exactly the hand patch already live in `chains.env`:
sonnet → `nvidia_nim/minimaxai/minimax-m3`, haiku →
`nvidia_nim/mistralai/mistral-medium-3.5-128b`.

## Acceptance criteria

- `expand_tier` promotes the first NIM cell to the head for `sonnet` and `haiku`
  when the capability head is non-NIM, preserving the order of the remaining
  entries and keeping `claude_code/<tier>` last.
- `expand_tier` leaves the chain unchanged when the head is already NIM.
- `expand_tier` leaves a non-`nim_head_tiers` tier (`opus`) untouched even when its
  head is non-NIM.
- `expand_tier` keeps the capability head and logs `mfc_nim_head_unavailable` when
  no NIM cell serves a `nim_head_tiers` tier.
- `expand_chains` threads `nim_head_tiers` through to every tier; the default
  applies the policy with no caller change.
- The existing `chain_expand` behaviour (NIM-first within a model, dedup, tail,
  servable index) is unchanged for everything else.
- `ruff check` / `ruff format` clean; no inline comments; full unit suite green.

## Architecture Impact

No new module, no new cross-component edge. The change is contained inside the
EXPAND-owned `chain_expand.py`. The live behavioural effect: regenerated sonnet and
haiku chains are headed by a free NIM cell, removing the paid-OpenRouter head on
the two highest-volume tiers and restoring the "NIM is the free head of
sonnet + haiku" invariant; opus stays the paid frontier tier.
