# SP-PROVIDER-REWIRE — re-register the direct providers

**Status:** specified
**Redesign slice:** C1 — after `SP-PROVIDER-CATALOG` (#407), before the
switchover. Umbrella: `docs/proxy_routing_redesign_architecture.md`.
**Touches forbidden paths:** no.

## Why

The #311 deep-cut left the registry with three providers, but the
operator holds live API keys for four more — **kimi (Moonshot), groq,
cerebras, deepseek**. The catalog slice made the registry extensible;
this slice fills it back. All four were probed live (2026-06-20) and
answer `GET /models` with their key on a standard OpenAI-compatible base:

| provider | base URL | sample live models |
|----------|----------|--------------------|
| kimi     | `https://api.moonshot.ai/v1`  | `kimi-k2.6`, `kimi-k2.7-code`, `moonshot-v1-128k` |
| groq     | `https://api.groq.com/openai/v1` | `qwen/qwen3-32b`, `openai/gpt-oss-*`, `whisper-large-v3` |
| cerebras | `https://api.cerebras.ai/v1`  | `gpt-oss-120b`, `zai-glm-4.7` |
| deepseek | `https://api.deepseek.com/v1` | `deepseek-v4-pro`, `deepseek-v4-flash` |

This unfreezes the failover fixtures that reference `kimi/` and `groq/`,
so the later switchover needs no relabeling.

## Change

Purely additive to the provider layer — no `model_router` / `routing`
edit. Each provider is one descriptor + one factory + a credential field,
all four sharing a single transport.

### `providers/openai_generic.py` (new)

`GenericOpenAIProvider(OpenAIChatTransport)` — for any upstream that
speaks OpenAI chat-completions with no provider-specific shaping. Its
`_build_request_body` is the shared `build_base_request_body` (the plain
Anthropic→OpenAI conversion, honouring the request's thinking flag).
Constructed with `provider_name` + `config.base_url` + `config.api_key`.

### `providers/defaults.py`

Add `KIMI_DEFAULT_BASE`, `GROQ_DEFAULT_BASE`, `CEREBRAS_DEFAULT_BASE`,
`DEEPSEEK_DEFAULT_BASE` (the probed URLs above).

### `providers/catalog.py`

Append four descriptors (`transport_type="openai_chat"`,
`capabilities=("chat","streaming","tools")`, `credential_env` +
`credential_attr` + `default_base_url`). `SUPPORTED_PROVIDER_IDS` grows
to seven automatically (it derives from the table).

### `providers/registry.py`

Add four factories returning `GenericOpenAIProvider(config,
provider_name=<id>)` via a small closure helper, wired into
`PROVIDER_FACTORIES`. The factory↔descriptor guard keeps them in sync.

### `config/settings.py`

Add `kimi_api_key` / `groq_api_key` / `cerebras_api_key` /
`deepseek_api_key` (`Field(default="", validation_alias=_aliases(...))`)
and the matching `_LEGACY_TO_FEROVA_ALIAS` entries
(`KIMI_API_KEY → FEROVA_KIMI_API_KEY`, etc.), so the keys already in
`.env` are read with the `FEROVA_` prefix winning.

## Acceptance

- New `tests/unit/test_openai_generic_providers.py`:
  - each new descriptor is present with `openai_chat` transport and its
    probed `default_base_url`;
  - `create_provider(<id>, settings)` with the key set returns a
    `GenericOpenAIProvider` whose `_base_url` matches the default;
  - `ModelRef.parse("kimi/kimi-k2.6")` (and groq/cerebras/deepseek) now
    succeeds — the catalog unfreeze realised;
  - `Settings` reads each key from its `FEROVA_*_API_KEY` env var.
- `tests/unit/test_provider_catalog.py` updated: the registry set is now
  the seven providers; every descriptor still has a factory.
- Full `pytest tests/unit` green; ruff + format clean; no inline
  comments; no silent except.

## Follow-on

`SP-ROUTING-SWITCHOVER`: repoint `ModelRouter`/`Settings` at
`RoutingTable`; the `kimi/`/`groq/` fixtures now validate, so the strict
switchover lands without relabeling. The operator may add the new
providers to `MODEL_*` chains in `chains.env` at will (config, not code).
