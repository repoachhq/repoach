---
id: SP-CHAINS-THINKING-CLASS
title: Machine-readable thinking class for chain models
version: 0.1
status: approved
author: jfaye (thinking-handling audit, 2026-07-04)
created: 2026-07-04
updated: 2026-07-04

owns:
  code: [src/repoach/llm_proxy/providers/catalog.py]
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Machine-readable thinking class for chain models

## Intent

Turn the thinking classification of chain models from prose comments
in `chains.env` (already drifted: the NO_THINKING lead rule is
violated by all three live heads) into machine-readable catalog
metadata, plus a read-only audit command that reports rule violations
— so the operator, and later chainpilot, can see thinking-class
facts instead of rotting comments.

## Context

`providers/catalog.py` describes each provider with informational
capability tags; only `nvidia_nim` and `open_router` carry a
"thinking" tag today and nothing branches on it. The live chains
(machine-regenerated `chains.env` values) are led by glm-5.2,
minimax-m3 and qwen3.7-max, none of which carries any thinking
annotation, while the file's own rule #1 says heads should be
NO_THINKING models. Chainpilot's planners take no thinking-class
input, so a chronic thinking-leaker can hold a chain head
indefinitely. This slice creates the metadata and the visibility; it
does NOT change chain generation.

## Goals

- G1: The catalog carries a `thinking_class` per (provider, model
  family) entry with values `reasoner` (always thinks), `hybrid`
  (toggleable), `non_thinking`, or `unknown` (default for unlisted
  models — honest, never guessed).
- G2: A pure function
  `audit_chain_thinking(chains: Mapping[str, list[str]]) -> list[str]`
  reports, one line per finding, every chain whose HEAD model is
  classified `reasoner` or `unknown` (rule #1 of `chains.env`), naming
  the chain, the model and its class.
- G3: A read-only CLI surface (`ferova proxy chains-audit` or an
  equivalent existing command extension) prints the audit lines and
  exits 0 always (report-only slice; enforcement is a later decision).
- G4: The initial classification covers every model in the live
  chains, sourced from the 2026-07-04 audit: glm-5.2 → hybrid,
  minimax-m3 → reasoner, qwen3.7-max → hybrid, deepseek-v4-pro →
  hybrid, kimi-k2.6 → reasoner, mistral-medium-3.5 → non_thinking,
  claude_code opus/sonnet/haiku → hybrid.

## Non-Goals

- NG1: No chain regeneration or reordering; no chainpilot planner
  change (a later slice consumes the metadata).
- NG2: No runtime request behaviour keyed off the class (the
  `REASONING_CONTROLS` provider matrix stays the only request-side
  policy).
- NG3: No `chains.env` format change.

## Assumptions

- A1: `src/ferova/llm_proxy/providers/catalog.py` is unowned in the
  arch registry (verified 2026-07-04: `owner_of` returns `None`).
- A2: Classification is keyed by model-id substring family matching
  (e.g. "minimax-m3" matches provider-prefixed aliases), mirroring how
  `aa_ingest.py` collapses model variants.

## Interface

Inputs:
- `audit_chain_thinking(chains)` — mapping of chain name to ordered
  model ids (as parsed from `chains.env`).

Outputs:
- `list[str]` of human-readable findings; empty when every head is
  `non_thinking` or `hybrid` with a known class.

Errors: none raised — unknown models classify as `unknown` and are
reported, never crashed on.

## Behavior

### Nominal

With today's live chains, the audit reports the minimax-m3-led chain
(reasoner head) and any head whose class is `unknown`; the operator
sees at a glance where rule #1 stands.

### Edge cases

- Empty chain list → no findings.
- Model absent from the classification table → `unknown`, reported.
- Provider-prefixed aliases of the same model → same class (family
  matching).

### Failure scenarios

- Malformed chain mapping (empty model list) → skipped with a
  finding naming the malformed chain, no exception.

## Architecture Impact

- No edge added or removed. `catalog.py` moves from the frontier into
  this spec's `owns.code`.

## Diagram

N/A (metadata table + pure audit function + CLI printer).

## Acceptance Criteria

- [ ] AC1: `tests/unit/test_provider_catalog.py::test_every_live_chain_model_has_a_thinking_class`
  — every model id appearing in the shipped chain constants resolves
  to a non-`unknown` class.
- [ ] AC2: `tests/unit/test_chains_thinking_audit.py::test_reasoner_head_is_reported`
  — a chain led by a `reasoner` model yields a finding naming chain,
  model and class.
- [ ] AC3: `tests/unit/test_chains_thinking_audit.py::test_non_thinking_head_is_clean`
  — a chain led by a `non_thinking` model yields no finding.
- [ ] AC4: `tests/unit/test_chains_thinking_audit.py::test_unknown_model_is_reported_not_guessed`
  — an unlisted model classifies as `unknown` and is reported.
- [ ] AC5: The full unit suite passes.

## Open Questions

(none)
