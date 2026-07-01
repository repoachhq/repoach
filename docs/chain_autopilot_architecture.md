# Chain Autopilot — Self-Maintaining Capability Chains

> Umbrella design for the arc that makes `chains.env` maintain *itself*:
> the factory observes its own model usage and outcomes, and keeps each
> capability chain functional and up to date as the model landscape moves.
> Slices land one governed spec at a time through the review factory.

## Why

`chains.env` is now the authoritative single source for the four
capability chains (SP-CHAINS-SINGLE-SOURCE, #422) — but it is **hand
curated**. The model landscape moves weekly: models reach EOL (HTTP 410),
go cold, start thinking-leaking, get faster or slower; new models appear
across providers; the same model behaves differently per provider. Keeping
four chains correct by hand, across a `(provider × model)` matrix that
changes constantly, is intractable and silently rots.

The breaker (SP-PROXY-HEALTH-BREAKER) already masks a bad model at runtime,
but it is **ephemeral**: it veils a dead cell TTL-bounded, it does not
change the *intentional* list. Nothing promotes a newly-healthy model,
demotes a chronically-weak one, or records *why*.

This arc closes that loop. It is the first genuinely **self-evolving**
piece of the system: the factory tunes its own plumbing from what it
observes. Automatic — but **documented**: every chain mutation leaves an
auditable record of *what changed, which signal triggered it, and what
replaced what*, so we never lose the why.

## Principles (agreed)

1. **Model ≠ provider.** A model is served by zero or more providers. The
   unit of evaluation is the **cell** `(provider, model)`, not the model
   alone — because the same model can be NO_THINKING on one provider and
   thinking-leak on another, with different latency and availability.
2. **A chain is model-centric.** A chain is an ordered list of *models*
   (by quality); each model fans out to **all its healthy providers**
   (redundancy). Failover therefore has two axes: across models (quality)
   and, within a model, across its providers (health/latency).
3. **Benchmarks are the prior; the factory's own outcomes are the truth.**
   Public benchmarks seed a never-seen model's quality. A model actually in
   service is judged by its real results on *our* tasks, which override the
   prior.
4. **Thinking is not a disqualifier.** The system must *handle* models that
   reason, not exclude them. (This reverses the current `chains.env` rule
   "only NO_THINKING models" — see Phase 0.)
5. **Attribution before eviction.** A failure is classified — model-fault
   vs provider-fault vs our-system-fault — before any action. Only a
   model-fault evicts the model everywhere; a provider-fault only skips
   that provider for that model; our-fault fixes us.
6. **Aggregate before judging.** One failure ≠ a bad model. Decisions run
   on rolling windows with a minimum sample size, so a single hard spec or
   a transient provider blip never evicts a good model.
7. **Automatic but documented.** The loop applies decisions itself, and
   every mutation writes a durable, human-readable rationale. The factory's
   existing ethos (findings-ledger, `arch check`) — the machine acts, the
   evidence persists.

## The system

```
                    ┌─────────────── catalog sweep ───────────────┐
                    │  iterate PROVIDER_DESCRIPTORS → /v1/models    │
                    │  → (provider × model) matrix                  │
                    └───────────────────┬──────────────────────────┘
                                        │
            benchmark prior ────────────┤
            (public rankings,           │
             name↔provider-id           ▼
             equivalences)        ┌─────────────┐    probe (per cell):
                                  │  candidate  │◄── alive / latency /
                                  │   matrix    │    thinking-handled
                                  └──────┬──────┘
            live perf harvest ──────────┤  (posterior overrides prior)
            coder: CI-green/rounds/stuck │
            reviewer: finding precision  ▼
                                  ┌─────────────┐
                                  │  decision   │── attribution +
                                  │   engine    │   rolling thresholds
                                  └──────┬──────┘
                                         │  proposed mutation
                                         ▼
                          audit log ──► apply ──► chains.env (authoritative)
                          (why)          (rewrite, model-centric)
```

Seven bricks:

1. **Catalog sweep** — iterate `PROVIDER_DESCRIPTORS`, query each
   provider's `/v1/models`, build the live `(provider × model)` matrix.
   Adding a provider tomorrow is one descriptor; the sweep picks it up.
   (`claude_code` is special-cased: a subprocess backstop, no `/v1/models`,
   never swept.)
2. **Benchmark prior** — ingest public benchmark rankings and resolve
   *equivalences* (benchmark model name ↔ each provider's real model ID,
   which differ). Gives a starting quality per tier for unseen models.
3. **Probe** — per cell: alive (not 410), warm (latency), and
   thinking-handled (not thinking-leak that starves output). Persisted,
   extending the existing `nim_health_probe` to the full matrix.
4. **Live performance harvest** — aggregate the factory's own outcomes
   **per model**: coder = CI-green rate / rounds-to-green / stuck rate;
   reviewer = finding precision (confirmed vs REFUTED), attributed via the
   recorded `model_used`. The posterior that corrects the benchmark prior.
   Each tier has its **own metric** — there is no single score.
5. **Decision engine** — attribution (model/provider/our-fault) +
   rolling-window thresholds with min-sample guards → evict / demote /
   promote, producing a *planned mutation* (pure; does not write yet).
6. **Audit log** — every applied mutation recorded: what, which signal,
   when, what it replaced. A queryable table + a human-readable changelog.
7. **Apply** — rewrite `chains.env` (safe to mutate since #422), in the
   model-centric structure, then emit the audit record. Orchestrated on a
   cadence by a routine.

## Phase 0 — prerequisite: the system must handle thinking

Principle 4 reverses a load-bearing assumption. Thinking models were
banned because hidden reasoning burned the whole token budget → empty
visible content → `peek_for_content` declared the candidate dead → false
failover cascades and timeouts. Un-banning them is **system work**, a
prerequisite to ever putting a thinking model in a chain:

- bound the reasoning budget on *every* transport so visible output is
  never starved (generalise the NIM-only cap);
- teach `peek_for_content` to distinguish "still producing
  `reasoning_content`" from "produced nothing" — a thinking stream is not a
  dead stream;
- a per-cell signal of whether thinking was handled cleanly.

The exact slices here are confirmed by a short audit of the current
thinking path (parts already exist: the NIM reasoning-budget cap, the
budget-retry #335, the thinking-block SSE parsing) — Phase 0 hardens what
is partial.

## Slice plan

Granular by design: small, mostly-additive specs — one new leaf module or
one focused change each, well under the autonomous Developer's LOC cliff,
so merge requests stay small and the Coder's per-iteration load stays
light. A topic spans several specs.

| # | SP-ID | Scope | Risk |
|---|-------|-------|------|
| 0a | `SP-CHAINPILOT-THINKING-AUDIT` | Audit the current thinking path; record the gaps; pin the real 0b/0c slices. Doc + characterization tests only. | low |
| 0b ✅ | `SP-CHAINPILOT-REASONING-CONTROL` (#425) | The headroom framework: `providers/reasoning.py` — `bounded_reasoning_budget`, the verified per-provider knob matrix, and `plan_reasoning`. Pure, additive. (was provisionally "THINKING-BUDGET"; the audit reshaped it.) | low — additive |
| 0b-2 ✅ | `SP-CHAINPILOT-REASONING-WIRE-TOKEN` (#426) | Wire the token-budget transports: NIM via the shared helper, OpenRouter default bound. | medium — live request shaping |
| 0b-3 | `SP-CHAINPILOT-REASONING-WIRE-GENERIC` | Wire the generic transport (kimi/groq/cerebras/deepseek): `max_tokens` floor + thinking-disable toggle (kimi/deepseek). **`reasoning_effort` DEFERRED to Phase 2** (it is per-model; cells unknown; transport unrouted). Folds 0c (the starved→retry→success spine already exists — `peek_for_content` separates budget-starved from dead and the dispatcher retries; the audit found no new failover logic is needed). | medium — live request shaping |
| 1a | `SP-CHAINPILOT-CATALOG-MODELS` | Per-descriptor `list_models()` hitting `/v1/models`; new leaf, additive. | low — additive |
| 1b | `SP-CHAINPILOT-MATRIX` | `(provider × model)` matrix domain type + builder over the catalog sweep. Pure types. | low — additive |
| 1c | `SP-CHAINPILOT-BENCHMARK-INGEST` | Ingest a public benchmark ranking into a local store. | low — additive |
| 1d | `SP-CHAINPILOT-EQUIVALENCES` | Resolver: benchmark name ↔ provider model ID. Pure logic + mapping store. | low — additive |
| 1e | `SP-CHAINPILOT-CHAIN-MODEL-CENTRIC` | Model-centric chain structure (ordered models × healthy providers), behind the existing flat `Chain` first. | medium — reshapes routing types |
| 2a | `SP-CHAINPILOT-PROBE-MATRIX` | Per-cell probe (alive/latency/thinking-handled), persisted across the full matrix. **Resolves the per-model `reasoning_effort` deferred from 0b-3** (which cells reason + which effort value each accepts) → feeds the generic transport's effort wiring. | medium — new probing |
| 2b | `SP-CHAINPILOT-CODER-OUTCOMES` | Harvest coder outcomes per model (CI-green/rounds/stuck) into a per-model store. Read over existing tables. | low — additive |
| 2c | `SP-CHAINPILOT-REVIEWER-OUTCOMES` | Harvest reviewer precision per model (extend slice-11, attribute by `model_used`). | low — additive |
| 2d | `SP-CHAINPILOT-PERF-AGGREGATE` | Rolling per-model score: benchmark prior + live posterior, min-sample guarded. Pure. | low — additive |
| 3a | `SP-CHAINPILOT-ATTRIBUTION` | Classifier: model-fault vs provider-fault vs our-fault, over failover/probe events. Pure. | medium — the hard core, isolated |
| 3b | `SP-CHAINPILOT-DECISION` | Decision engine: thresholds → planned mutation (evict/demote/promote). Pure; no write. | medium |
| 3c | `SP-CHAINPILOT-AUDIT-LOG` | Mutation journal (table + changelog writer). | low — additive |
| 3d | `SP-CHAINPILOT-APPLY` | Apply a planned mutation to `chains.env` + emit the audit record. | high — mutates the live source |
| 3e | `SP-CHAINPILOT-LOOP` | Orchestrate the whole loop on a cadence (routine). | medium — wiring |

Sequencing: **0 → 1 → 2 → 3**. Within a phase, the additive slices land in
any order; the delicate ones (0b/0c, 1e, 3d) are isolated so they each
carry a single risk. Each phase produces value on its own — Phase 1+2 give
an *observatory* (the matrix, the per-model scoreboard) even before any
automatic mutation in Phase 3.

## Out of scope

- The multi-turn agent loop and the peek-then-replay buffering strategy
  (unchanged).
- A bespoke quality-eval harness — by Principle 3 the factory's real work
  *is* the eval; we harvest, we do not synthesise benchmarks.
- Self-modification beyond the chains (the loop tunes `chains.env`, not the
  factory's own code).
</content>
