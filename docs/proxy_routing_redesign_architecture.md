# LLM Proxy Redesign — A Routing Domain & a Closed Health Loop

- **Status**: direction agreed by the operator on 2026-06-19. This
  document is the target architecture and the slice plan; each slice
  ships as its own spec through the factory.
- **Shape**: targeted rewrite — a new `llm_proxy/routing/` domain layer
  replaces the string-based routing internals; the failover loop,
  providers, and SSE core are kept and re-wired behind a preserved
  boundary type (`ResolvedModel`).
- **Sequence agreed**: pillar 2 (domain model) first, pillar 1 (health
  loop) next, pillar 3 (provider SPI) last.

## Why

The session-start probe on 2026-06-19 told the whole story: `sonnet`
(`mistral-medium-3.5`) returned a `ReadTimeout` and `coder`
(`qwen3-coder-480b`) returned `http=410`. **Both stay at the head of
their chains for the rest of the day.** Every review-bot request hits
the dead head first, pays the full timeout, then fails over — per
request, with no memory.

One structural flaw underneath: **the chain is static and health is an
open loop.** The proxy walks the same configured chain on every request
(`ModelRouter.resolve_chain` → `Settings.resolve_models`,
`model_router.py:54-86`). The health probe writes `nim_health_probe`
rows to SQLite but **nothing reads them back** — `fetch_probes()` has no
caller. Detection exists; it just never reaches the routing decision.

Around that core sit three avoidable costs, each a symptom of the same
missing abstraction — there is no domain model:

- **Tiers are substring matches on model names** (`"coder" in name`,
  `"opus" in name`; `settings.py:411-420`). No `Tier` type.
- **Chains are comma-split strings** (`_split_chain`, `settings.py:366`)
  re-parsed and re-wrapped into `ResolvedModel` in a second place
  (`model_router.py:88-95`). No `Chain` type.
- **A `provider/model` ref is a bare string** parsed ad hoc by
  `parse_provider_type` / `parse_model_name` (`settings.py:422-430`) in
  several call sites. No `ModelRef` type.

Give routing a domain model and the health loop closes in one method;
leave it as strings and every future guard is another special case.

## Principles (agreed)

1. **`ResolvedModel` is the boundary, and it does not change.** The
   failover loop (`services.py`), the peek oracle (`_failover.py`), and
   the providers consume `ResolvedModel`. The rewrite lives entirely
   *behind* it. Switchover is behaviour-preserving by construction.
2. **One source of truth for routing.** Tier classification and chain
   parsing collapse into a single `RoutingTable` built once from
   `Settings`. `resolve_models` / `resolve_model` / `_split_chain` and
   the duplicate `ResolvedModel` builder are deleted, not wrapped.
3. **A ref is a value, not a string.** `ModelRef` validates against the
   provider registry at construction; an invalid `provider/model` can no
   longer travel through the system as a plain string.
4. **Health is a closed loop.** Live failover signals (410, timeout,
   empty) and the probe feed one breaker state; the router filters the
   chain through it instead of re-trying a known-dead head.
5. **Parity is proven, not assumed.** The switchover ships with
   characterization tests that pin the current chains from `chains.env`
   to their resolved output, so the rewrite is provably equivalent
   before the breaker changes any behaviour.

## Pillar 2 — the routing domain (first)

New module, clean-slate, no behaviour change until the switchover slice.

```
llm_proxy/routing/
  refs.py     ModelRef (Pydantic v2), ProviderId
  tier.py     Tier, classify_tier(model_name) -> Tier
  chain.py    Chain
  table.py    RoutingTable
```

### `ModelRef` — Pydantic v2 value object

Aligns with the CLAUDE.md rule (Pydantic for anything crossing a module
boundary). Frozen, validated against `SUPPORTED_PROVIDER_IDS`.

```
ModelRef
├─ provider_id : ProviderId        # nvidia_nim | open_router | claude_code
├─ model       : str               # everything after the first "/"
├─ classmethod parse("nvidia_nim/mistralai/mistral-medium-3.5-128b")
├─ __str__  -> "provider/model"    # round-trips chains.env verbatim
└─ to_resolved(original_model) -> ResolvedModel   # the boundary adapter
```

`ProviderId` is a `StrEnum` whose members are exactly
`SUPPORTED_PROVIDER_IDS` (`config/provider_ids.py`) — the existing
registry sync assertion (`providers/registry.py:121-128`) extends to
cover it, so the id set stays single-sourced.

### `Tier` & `classify_tier`

```
Tier = OPUS | SONNET | HAIKU | CODER | DEFAULT
classify_tier(model_name) -> Tier
```

The substring logic from `resolve_models` (`settings.py:407-420`),
extracted verbatim and unit-tested in isolation — including the H10
explicit-`provider/...` passthrough and the first-match-wins order
(`coder` before `opus` before `haiku` before `sonnet`).

### `Chain` & `RoutingTable`

```
Chain                      # ordered, non-empty, de-duplicated
├─ refs : tuple[ModelRef]
├─ without(skip: frozenset[ModelRef]) -> Chain   # never empty (falls back to head)
└─ (later) without_down(breaker) -> Chain

RoutingTable               # Tier -> Chain, built once from Settings
├─ classmethod from_settings(settings) -> RoutingTable
└─ chain_for(model_name) -> Chain     # classify_tier + the tier's chain
```

`RoutingTable.from_settings` parses each `MODEL_*` slot once at build
time (replacing per-request `_split_chain`), so a malformed ref fails
loudly at startup, not mid-failover.

## The switchover seam

`ModelRouter.resolve_chain` becomes a thin adapter; `ResolvedModel`
output is unchanged:

```python
chain = self._table.chain_for(name).without(skip)
return [ref.to_resolved(name) for ref in chain]
```

`Settings.resolve_models` / `resolve_model` / `_split_chain` are
removed; their only callers are the router and tests, both repointed at
`RoutingTable`. `skip_models` (`frozenset[str]`) is parsed to
`frozenset[ModelRef]` at the adapter edge so the semantic-failover
contract (`SP-PROXY-SEMANTIC-FAILOVER`) is preserved.

Untouched by this pillar: `services.py`, `_failover.py`, every provider,
`core/anthropic/*`.

## Pillar 1 — close the health loop (next)

Once `Chain` is first-class, the breaker is additive:

- `routing/breaker.py` — an in-process `BreakerState`: `ModelRef -> down
  until T`, fed by (a) the live failover classifier already computed in
  `services.py:37-75` (`provider_410`, `timeout`, empty) and (b) the
  probe rows the proxy can read at startup (`nim_health_probe`).
- `Chain.without_down(breaker)` filters the head before dispatch; a
  fully-down chain still yields its head (loud failure beats empty).
- TTL-bounded so a recovered model re-enters automatically; the breaker
  is a hint, live verification at dispatch stays the truth.

This is where the 410/ReadTimeout of 2026-06-19 stops being re-tried
first on every request.

## Pillar 3 — provider SPI (last)

Factor the three providers (3 request-builders, 3 thinking strategies,
3 error-event emitters, 3 streaming loops; see the providers map) behind
a small SPI so a new provider is one class. Scoped separately; no
dependency on pillars 1–2 beyond `ModelRef`.

## Slice plan

| Slice | SP-ID | Scope | Risk |
|-------|-------|-------|------|
| A ✅ | `SP-ROUTING-DOMAIN-TYPES` | `refs.py` + `tier.py` + tests. Pure types, zero wiring. (#405) | low — additive |
| B ✅ | `SP-ROUTING-TABLE` | `chain.py` + `table.py` + `from_settings` + tests. Not yet wired. (#406) | low — additive |
| C0 | `SP-PROVIDER-CATALOG` | **Inserted prerequisite.** Make the provider set extensible: a single leaf descriptor catalog drives `SUPPORTED_PROVIDER_IDS`; drop the hand-maintained `ProviderId` enum, `ModelRef` validates against the catalog. | low — additive + unwired ProviderId reshape |
| C1 | `SP-PROVIDER-REWIRE` | Re-register the providers stripped by the #311 deep-cut — `kimi` / `groq` / `cerebras` / `deepseek` (live API keys held): descriptors + factories + clients + credential fields. | medium — new providers |
| C | `SP-ROUTING-SWITCHOVER` | `ModelRouter`/`Settings` delegate to `RoutingTable`; delete the duplicated string logic; characterization parity tests. With the real providers registered, no fixture relabeling. | medium — touches live routing |
| D | `SP-PROXY-HEALTH-BREAKER` | `breaker.py` + `Chain.without_down` + feed from failover classifier & probe. | medium — changes behaviour (intended) |
| E | `SP-CHAINS-SINGLE-SOURCE` | resolve the `chains.env` ↔ `.env` drift; one canonical source. | separate, optional |
| F+ | `SP-PROVIDER-SPI-*` | pillar 3, factor the provider layer. | large, later |

> **2026-06-19 re-sequencing.** The operator flagged that the strict
> `ProviderId(3)` froze the registry at the #311 survivors, rejecting
> `kimi`/`groq`/`cerebras`/`deepseek` (real, credentialed providers). The
> registry is made **extensible first** (C0), the real providers
> **re-wired** (C1), and only then the strict switchover (C) — so it needs
> no test-fixture relabeling. This realises umbrella principle 3 properly:
> "a ref validates against the registry" means the *live* registry, not a
> frozen snapshot.

A and B are small and additive (within the autonomous Developer's
capacity). C is the delicate one (switchover + parity) and is the likely
hand-implemented slice.

## Out of scope

- Multi-turn agent loop (lives in `repoach.llm.gateway`, unchanged).
- The peek-then-replay buffering strategy (`_failover.py`) — noted as a
  latency cost, not addressed by this arc.
- The `.env`/`chains.env` precedence machinery, except slice E.
