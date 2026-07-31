---
id: SP-BYO-PROVIDERS
title: Bring-your-own model — direct openai / anthropic providers + a generic openai-compatible endpoint
version: 0.1
status: approved
author: agent
created: 2026-07-31
updated: 2026-07-31

owns:
  code: [tests/unit/test_byo_providers.py]
  resources: N/A

depends_on: [SP-CHAINS-THINKING-CLASS, SP-PROXY-FIRST-BYTE-DEADLINE, SP-CHAIN-STATUS-PROXY-DEFAULT]
provides_to: []

constraints: {}
---

# Bring-your-own model — direct openai / anthropic providers + a generic openai-compatible endpoint

## Intent

A third party who wants to run the review factory on **their own** model
today has a gap: the provider catalog wires 7 ids
(`nvidia_nim, open_router, claude_code, kimi, groq, cerebras, deepseek`)
but there is **no direct `openai` provider, no direct `anthropic`
provider, and no way to point at a self-hosted OpenAI-compatible endpoint**
(Ollama / vLLM / LM Studio / any aggregator). Someone holding a raw OpenAI
or Anthropic key must route through OpenRouter, and someone running a local
model has no path at all without editing source. This closes that gap by
adding three descriptor entries — reusing the descriptor + generic-transport
machinery that already exists — so "bring your own model" is pure `.env` +
`chains.env` configuration for the common cases, with `claude_code` remaining
a genuinely optional hop a third party can delete entirely.

## Context

- `src/repoach/llm_proxy/providers/catalog.py`: `PROVIDER_DESCRIPTORS`
  is a `dict[str, ProviderDescriptor]`; a `ProviderDescriptor` already
  carries `provider_id`, `transport_type` (`"openai_chat"` |
  `"anthropic_messages"`), `credential_env`, `credential_url`,
  `credential_attr`, `static_credential`, `default_base_url`,
  `base_url_attr`, `proxy_attr`, and `capabilities`. Adding a provider is
  adding a descriptor entry — no new transport class is needed for an
  OpenAI-compatible or Anthropic-messages endpoint.
- `src/repoach/llm_proxy/providers/registry.py:117`:
  `base_url = _string_attr(settings, descriptor.base_url_attr,
  descriptor.default_base_url or "")` — a descriptor whose
  `base_url_attr` names a `Settings` field already reads its base URL from
  that field, so a configurable endpoint needs only a new Settings field
  plus a descriptor that points at it.
- `src/repoach/llm_proxy/config/provider_ids.py`: `SUPPORTED_PROVIDER_IDS`
  is derived from `PROVIDER_DESCRIPTORS`, so new descriptors become valid
  `provider/model` refs automatically (`routing/refs.py:43-47` validates
  against it).
- `src/repoach/llm_proxy/config/settings.py:239-246`: per-provider keys
  follow one pattern —
  `<name>_api_key: str = Field(default="", validation_alias=_aliases("<NAME>_API_KEY"))`.
- `claude_code` is already optional: `ProviderRegistry.get()` lazily
  constructs a provider only when its id appears in a resolved chain, so a
  chain with no `claude_code/...` entry never touches it (this spec does
  not change that — it just gives non-claude_code users first-class keys).

## Goals

- G1: a `openai` provider descriptor — `transport_type: "openai_chat"`,
  `credential_env: "OPENAI_API_KEY"`, `credential_attr: "openai_api_key"`,
  `default_base_url: "https://api.openai.com/v1"`,
  `credential_url` pointing at the OpenAI keys page, capabilities
  `("chat", "streaming", "tools")` — so `openai/gpt-4o` (etc.) is a valid
  chain ref that dispatches against a raw OpenAI key.
- G2: an `anthropic` provider descriptor —
  `transport_type: "anthropic_messages"`,
  `credential_env: "ANTHROPIC_API_KEY"`,
  `credential_attr: "anthropic_api_key"`,
  `default_base_url: "https://api.anthropic.com"`, capabilities
  `("chat", "streaming", "tools", "native_anthropic")` — so
  `anthropic/claude-sonnet-4-5` dispatches against a raw Anthropic key.
- G3: an `openai_compatible` provider descriptor —
  `transport_type: "openai_chat"`,
  `credential_env: "OPENAI_COMPATIBLE_API_KEY"`,
  `credential_attr: "openai_compatible_api_key"`,
  `base_url_attr: "openai_compatible_base_url"` (no `default_base_url`) —
  so a third party sets `REPOACH_OPENAI_COMPATIBLE_BASE_URL` to any
  OpenAI-compatible endpoint (Ollama `http://localhost:11434/v1`, vLLM,
  LM Studio, Together, Fireworks, …) and uses `openai_compatible/<model>`.
- G4: the three new `Settings` fields
  (`openai_api_key`, `anthropic_api_key`, `openai_compatible_api_key`,
  `openai_compatible_base_url`) are added to
  `llm_proxy/config/settings.py` following the existing `_aliases(...)`
  pattern; `openai_compatible_base_url` defaults to `""`.
- G5: `SUPPORTED_PROVIDER_IDS` now contains `openai`, `anthropic`,
  `openai_compatible`, and the registry can build all three (they use
  existing generic transports, so `_UNBUILDABLE_DESCRIPTORS` stays empty).

## Non-Goals

- NG1: no change to the 7 existing descriptors, to any existing chain, or
  to `chains.env`'s shipped defaults — a fresh clone behaves identically.
- NG2: no new transport class — the three providers reuse the existing
  `openai_chat` / `anthropic_messages` generic builders.
- NG3: no removal or change of `claude_code` — it stays the optional
  subscription hop.
- NG4: no auto-selection / no change to failover, breaker, or capability
  tiering — a third party still authors their own `chains.env`.
- NG5: no docs rewrite here (getting-started / README additions are a
  separate editorial follow-up); this spec is the code capability.

## Interface

`src/repoach/llm_proxy/providers/catalog.py` — three new
`PROVIDER_DESCRIPTORS` entries (`openai`, `anthropic`, `openai_compatible`)
shaped exactly like the existing ones.

`src/repoach/llm_proxy/config/settings.py` — new fields:

```python
openai_api_key: str = Field(default="", validation_alias=_aliases("OPENAI_API_KEY"))
anthropic_api_key: str = Field(default="", validation_alias=_aliases("ANTHROPIC_API_KEY"))
openai_compatible_api_key: str = Field(default="", validation_alias=_aliases("OPENAI_COMPATIBLE_API_KEY"))
openai_compatible_base_url: str = Field(default="", validation_alias=_aliases("OPENAI_COMPATIBLE_BASE_URL"))
```

No new function signatures — the descriptor + registry machinery consumes
these automatically.

## Behavior

### Nominal
- `chains.env` with `MODEL_SONNET=openai/gpt-4o` + `OPENAI_API_KEY` in
  `.env` → the proxy dispatches sonnet-tier calls to OpenAI directly.
- `MODEL_HAIKU=openai_compatible/llama3.1` +
  `REPOACH_OPENAI_COMPATIBLE_BASE_URL=http://localhost:11434/v1` → dispatches
  to a local Ollama server; `OPENAI_COMPATIBLE_API_KEY` may be empty for
  keyless local endpoints (the generic transport sends whatever is set).

### Edge cases
- `openai_compatible/<model>` with no `REPOACH_OPENAI_COMPATIBLE_BASE_URL`
  set: the resolved base URL is `""` and the first dispatch fails with the
  transport's existing clear connection error (not a silent misroute) —
  acceptable, it is a misconfiguration the operator sees immediately.
- A chain referencing `openai/<model>` with no `OPENAI_API_KEY`: the
  existing `_credential_for` / registry path raises the same clear
  "missing key, get one at <url>" `AuthenticationError` as every other
  provider (registry.py:104-110).

### Failure scenarios
- None new — the three providers ride the existing generic transports,
  breaker, and failover exactly like `kimi`/`groq`.

## Acceptance Criteria

- [ ] AC1: `SUPPORTED_PROVIDER_IDS` contains `openai`, `anthropic`,
  `openai_compatible`; `ModelRef("openai", "gpt-4o")` and the other two
  parse without the "unknown provider" `ValueError`.
- [ ] AC2: the registry builds each of the three (no `_UNBUILDABLE_DESCRIPTORS`
  entry) and the built provider carries the expected transport type and
  resolved base URL (openai → api.openai.com, anthropic → api.anthropic.com,
  openai_compatible → the value of `REPOACH_OPENAI_COMPATIBLE_BASE_URL`).
- [ ] AC3: with `REPOACH_OPENAI_COMPATIBLE_BASE_URL` set, a built
  `openai_compatible` provider dispatches against that base URL (assert via
  a truthful `httpx.MockTransport` boundary fake that the request went to
  the configured host).
- [ ] AC4: promised test file `tests/unit/test_byo_providers.py` with the
  above selectors; the tests FAIL on pre-change code (the three ids are
  unknown / the Settings fields are absent).
- [ ] AC5: `ruff` + the lint gates green; the shipped 7-provider chains and
  every existing provider test stay green (NG1).

## Architecture Impact

- Extends `PROVIDER_DESCRIPTORS` + `Settings` only — no new module, no new
  transport, no coupling change. Reuses the descriptor/registry seam that
  SP-PROVIDER-REWIRE / SP-CHAINS-THINKING-CLASS built.
- Turns "bring your own model" from a source change into `.env` +
  `chains.env` config for OpenAI, Anthropic, and any OpenAI-compatible
  endpoint — the key third-party-adoption unblock.
