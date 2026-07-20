# Model-first chains — architecture

> Status: **design approved, implementation not started** (2026-06-30).
> Umbrella for the `SP-MFC-*` slice family. Individual governed specs live in
> `docs/specs/2026-06-30_SP-MFC-*`.

## Objective

Invert the chain-construction primitive. Today `chains.env` hardcodes ordered
`provider/model` entries **by hand** (and the chainpilot mechanically edits
them). The new design chooses **models first** — by capability, sourced
automatically from a public benchmark — then expands each chosen model to
**every provider that serves it**. `chains.env` becomes a **generated artifact**,
never hand-edited; its true source is an ordered model list per tier derived
from the Artificial Analysis intelligence index.

This is the first piece to make the routing layer self-maintaining off the
real model market rather than off a hand-curated snapshot. It reframes the
Chain Autopilot from *"edit chains.env"* to *"maintain the ranking + matrix,
regenerate chains.env"*.

## The two-level chain

A chain is **models ordered by capability**, and within each model **all the
providers that serve it**:

```
model A (best capability)  → [nim/A, open_router/A, …every serving provider]
model B                    → [nim/B, open_router/B, …]
…
claude_code/<tier>         (net — and the reference that defines the tier band)
```

Failover is therefore two-level: exhaust **all providers of model A** (same
capability, a single upstream merely fell over) before dropping to model B.
Capability degrades only once the best model is dead **everywhere**.

## The construction algorithm (per tier: opus / sonnet / haiku)

```
1. INGEST     Artificial Analysis free API → per-model intelligence/coding/agentic
              index + price + median performance. Collapse a model's
              reasoning/non-reasoning variants to ONE capability = MAX across them.
2. GATE       candidate pool = models served by ≥1 of OUR providers
              (provider matrix ∩ AA dataset, joined via the equivalence table).
              This gate runs BEFORE top-N — a global top-N first would pick
              unservable flagships (GPT-5.5, Gemini) that expand to zero providers.
3. ELIGIBLE   index ≥ index(claude-<tier>) − margin     (bands are nested)
4. SELECT     opus   = top-N by intelligence            (opus band)
              sonnet = top-N by intelligence in [claude-sonnet, claude-opus)
              haiku  = top-N by SPEED among haiku-eligible ("fast but not dumb")
5. EXPAND     each selected model → matrix["who serves it?"];
              order NIM first (free) then by descending LIVE cell-probe speed;
              if NIM does not serve it, the fastest server heads the block.
6. TAIL       append claude_code/<tier>.
```

## Pinned parameters (operator, 2026-06-30)

| Knob | Value | Note |
|------|-------|------|
| margin | **−5** index pts below the Claude anchor | configurable; cheap to be inclusive since NIM-free heads cost nothing |
| N (depth) | **opus 5 · sonnet 4 · haiku 3** | per-tier |
| variant collapse | **MAX across reasoning/non-reasoning variants** | selection is variant-agnostic ("we handle models that think"); anchor Claude the same way → the ≥-comparison stays like-for-like |
| collapse key | normalized model **name** | the AA `slug` does NOT unify reasoning vs non-reasoning rows |
| anchor (live 2026-06-30) | opus 53.5 · sonnet 47.2 · haiku 29.6 | Claude Opus 4.7 / Sonnet 4.6 / 4.5 Haiku, max-variant |
| thresholds | opus ≥48.5 · sonnet ∈[42.2,48.5) · haiku ≥24.6 | anchor − margin |

## Strategic decision A — opus is the frontier, paid

Live provider-coverage probe (`/v1/models` × AA caps, 2026-06-30):

- **NIM (free) caps at ~44** (minimax-m3 44.4, deepseek-v4-pro 44.3, kimi-k2.6
  42.8). **Zero NIM models reach the opus threshold (48.5).** NIM fills sonnet
  (3 in band) and haiku (14).
- Only **OpenRouter** serves opus-tier models (≥48.5) — 17, all **paid
  flagships** (claude-fable-5 59.9, opus-4.8 55.7, gpt-5.5 54.8).

The collapse-MAX choice pushes the opus threshold to 48.5, which prices the free
tier out of opus. The operator chose **A**: opus = frontier capability, **paid**
(OpenRouter flagships + the claude_code tail); NIM-first does not apply at opus.
NIM stays the free head of sonnet + haiku. Rationale: pay for quality only on the
hardest, least-used tier (coding agents are SONNET since the CODER-tier
retirement). Rejected **B** (lower the anchor / median-collapse so NIM heads
opus) as relabeling weaker models "opus".

## Artificial Analysis API (verified live 2026-06-30)

- Key: `.env` → `REPOACH_ARTIFICIAL_ANALYSIS_API_KEY`. Auth header **`x-api-key`**
  (not Bearer).
- Free endpoint: `GET https://artificialanalysis.ai/api/v2/language/models/free`
  — HTTP 200, **paginated** (`page`/`page_size`; ~518 models across 3 pages).
- Per-model fields: `name`, `slug`, `model_creator`,
  `evaluations.{artificial_analysis_intelligence_index, coding_index,
  agentic_index}` (coding/agentic sometimes `null` → fall back to intelligence),
  `pricing.{price_1m_input_tokens, price_1m_output_tokens}`,
  `performance.{median_output_tokens_per_second,
  median_time_to_first_token_seconds, median_end_to_end_response_time_seconds}`.
  Payload also carries `intelligence_index_version` — the index re-baselines,
  which is **why we anchor on Claude, not on absolute thresholds**.
- The full `/language/models` and ALL `/providers/*` endpoints are
  **Pro/Commercial only (403 on free)**. So AA gives MODEL-level capability +
  reference price/perf only; **per-provider speed (NIM vs OpenRouter for the same
  model) is NOT available** → provider ordering uses our **live cell-probe**.

## Foundation already in place (Chain Autopilot)

- `llm_proxy/providers/model_catalog.py`, `model_matrix.py` — the `(provider ×
  model)` matrix.
- `benchmark_equivalences.py` + json — the `name ↔ canonical ↔ id` resolver
  (the join linchpin; today only approximately covers the live catalogs).
- `providers/cell_probe.py` + sweep + `cell_health_probe` table — live per-cell
  speed/health.
- `review/chain_rewrite.py` — mechanical chains.env writer (flag-gated atomic).
- `benchmark_prior.py` + json — the MANUAL static snapshot this arc replaces with
  the live AA ingest.

## Slice plan (governed `SP-MFC-*`)

1. **SP-MFC-AA-INGEST** — Artificial Analysis free-API client: paginated fetch,
   schema + sanity-bound validation, variant collapse (max-per-model). Pure leaf,
   additive/unwired. *(spec written.)*
2. **SP-MFC-SELECT** — eligibility (Claude-anchored thresholds + margin) +
   tier-specific top-N, gated by the servable matrix. depends_on: AA-INGEST,
   model-matrix, equivalences.
3. **SP-MFC-EXPAND** — provider expansion per model (matrix lookup, NIM-first then
   live cell-probe speed, claude_code tail). depends_on: SELECT, model-matrix,
   cell-probe, equivalences.
4. **SP-MFC-GENERATE** — assemble the three tier chains and write `chains.env`
   (flag-gated atomic, shadow by default). depends_on: EXPAND; declarative edge to
   the `format:capability-chains` contract (owned by chains.env, #422).
5. **SP-MFC-CHAINPILOT-REFRAME** — repoint the chainpilot from mechanical edits to
   ranking/matrix maintenance + regeneration. Touches live armed config →
   atomic, armed-aware, adversarially reviewed.

## Open / to firm up during implementation

- **The equivalence join** (`id ↔ canonical ↔ name`) is the linchpin and is only
  approximate today (NIM 37/121 matched by substring). SELECT/EXPAND must
  strengthen it or accept measured coverage loss (and `log()` what was dropped).
- **Regeneration trigger** — proxy startup vs a CLI command vs the chainpilot 6h
  timer (settled in GENERATE / REFRAME).
- Margin and N are configurable; revisit on real generated chains.
```
