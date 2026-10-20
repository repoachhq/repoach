---
id: SP-UNIVERSAL-PROVIDERS
title: Bring-your-own key by default, the chain-failover gateway optional, a usage ledger per project
version: 0.1
status: draft
author: agent
created: 2026-10-18
updated: 2026-10-18

owns:
  code: [src/repoach/providers/, tests/unit/test_provider_select.py, tests/unit/test_usage_ledger.py]
  resources: [db:table:usage_ledger]

depends_on: [SP-UNIVERSAL-INSTALL, SP-UNIVERSAL-PROJECT-CONFIG, SP-BYO-PROVIDERS, SP-CHAINS-SINGLE-SOURCE]
provides_to: []

constraints: {}
---

# Bring-your-own key by default, the gateway optional

## Intent

A user with an Anthropic API key, a Claude Code OAuth token or an
OpenAI-compatible endpoint runs the review team with no gateway
process. The chain-failover gateway stays available as
`provider = "gateway"` for whoever wants failover across free
providers. Every call is recorded in a per-project usage ledger, and a
missing provider fails with one clear message.

## Context

- `src/repoach/agent_engine/agent_loop.py:422-430` builds a
  `ProxyGatewayClient` from `settings.llm_proxy_base_url`; the loop
  speaks `repoach/v1` (`AgentRequest` → `AgentResponse`,
  `adapters.py:1-16,56-80`). There is no in-process alternative.
- `SP-BYO-PROVIDERS` added `openai`, `anthropic`, `openai_compatible`
  descriptors *inside the gateway* (`llm_proxy/config/settings.py:252-259`,
  `providers/catalog.py`): a key still needs the gateway running.
- `claude_code` shells out to `claude --print --output-format json
  --model <tier>` (`llm_proxy/providers/claude_code/client.py:183-191`)
  and bills the Max plan; the Yoob precedent for a headless token is
  `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`
  (`yoob_2/ci/autofix/autofix/agent.py:105-114`).
- Measured 2026-10-18: every free chain head answers HTTP 410, OpenRouter
  credit is negative; the only hop that answers is `claude_code/<tier>`.
- Tokens are recorded per review in `pr_reviews.tokens_used`
  (`review/persistence.py:64,80,247`); there is no per-call, per-project
  usage table and no cost column.
- The `anthropic` and `openai` SDKs are already dependencies
  (`pyproject.toml`).

## Goals

- G1: a `Provider` protocol (`src/repoach/providers/protocol.py`) with one method, `complete(request: AgentRequest) -> AgentResponse`, the exact seam `ProxyGatewayClient.call` fills today; the agent loop takes a `Provider`, not a base URL.
- G2: four shipped providers: `AnthropicProvider` (SDK, `REPOACH_ANTHROPIC_API_KEY`), `ClaudeCodeProvider` (in-process reuse of the gateway's subprocess client, `CLAUDE_CODE_OAUTH_TOKEN` or the interactive login), `OpenAICompatibleProvider` (SDK, `REPOACH_OPENAI_API_KEY` + `REPOACH_OPENAI_BASE_URL`, covers OpenAI, Ollama, vLLM), `GatewayProvider` (today's `ProxyGatewayClient`, `REPOACH_LLM_PROXY_BASE_URL` + token).
- G3: selection: `--provider` > `REPOACH_PROVIDER` > `[provider].kind`; tier model ids from `[provider].opus/sonnet/haiku` or `REPOACH_MODEL_{OPUS,SONNET,HAIKU}`, with per-provider defaults (`anthropic`: `claude-opus-4-7`/`claude-sonnet-4-6`/`claude-haiku-4-5`; `claude_code`: `opus`/`sonnet`/`haiku`; `openai_compatible`: none — required; `gateway`: the alias the chain router classifies, unchanged).
- G4: no provider resolvable → `NoProviderConfiguredError`, rc 2, stderr: `no provider configured — set REPOACH_PROVIDER=anthropic|claude_code|openai_compatible|gateway, or [provider].kind in repoach.toml; run 'repoach doctor'`. Only `provider = "gateway"` triggers `require_llm_proxy_base_url`.
- G5: a `usage_ledger` table (project_slug, ts, command, pr_number, role, provider, model, input_tokens, output_tokens, cached_tokens, elapsed_s, cost_usd nullable, error nullable) written by every `complete()`; `repoach usage [--since] [--json]` prints per-command and per-model totals; `cost_usd` is null when the provider reports no price, never estimated silently.
- G6: `claude_code` is never auto-selected: a user who only has the `claude` binary gets the no-provider message with the hint, not a silent Max-plan charge.

## Non-Goals

- NG1: no failover across providers outside the gateway; a direct provider's error is the review's error.
- NG2: no change to `chains.env`, the chain router, breaker, effort map, chainpilot (umbrella NG3).
- NG3: no streaming; the loop consumes whole responses as today.
- NG4: no price table shipped; cost is recorded only when the provider's response carries it.

## Assumptions

- A1: `AgentRequest`/`AgentResponse` (`llm_proxy/api/models/agent_v1.py`) stay the loop's wire models; the direct providers translate to SDK calls in-process, including tool specs and `ThinkingConfig`.
- A2: the Anthropic SDK's `messages.create` returns `usage` with input/output/cache tokens; OpenAI-compatible endpoints may omit `usage` (recorded as null).

## Interface

```python
class Provider(Protocol):
    name: str
    def complete(self, request: AgentRequest, *, tier: CapabilityTier) -> AgentResponse: ...
    def describe(self) -> ProviderInfo: ...

def select_provider(config: ProjectConfig, *, override: str | None = None) -> Provider: ...
def record_usage(db: Engine, row: UsageRow) -> None: ...
```

Inputs:
- `REPOACH_PROVIDER`: str — one of the four names.
- `REPOACH_ANTHROPIC_API_KEY`, `CLAUDE_CODE_OAUTH_TOKEN`, `REPOACH_OPENAI_API_KEY`, `REPOACH_OPENAI_BASE_URL`, `REPOACH_LLM_PROXY_BASE_URL`, `REPOACH_ANTHROPIC_AUTH_TOKEN`: str — per provider, from the environment or `user_config_dir()/env`.

Outputs:
- `repoach usage`: a table `command | provider | model | calls | in_tokens | out_tokens | cost_usd` with `cannot say` where cost is null.
- `repoach doctor`: the `provider=` line names the selected provider and whether its credential is present (never its value).

Errors:
- `NoProviderConfiguredError`: rc 2.
- `ProviderAuthError`: credential rejected — rc 2, provider message, no retry.
- `ProviderTransportError`: 5xx/429/timeout — retried per the loop's existing backoff, then rc 2.

## Behavior

### Nominal
`REPOACH_PROVIDER=anthropic REPOACH_ANTHROPIC_API_KEY=… repoach review pr
1171 --dry-run` → four reviewers, refuter and verifiers run against the
API; four `usage_ledger` rows per reviewer round plus one per refuter
call; no process listens on any port.

### Edge cases
- `provider = "gateway"` with no URL → today's `llm_proxy_base_url unresolved` message, rc 2, unchanged wording.
- `claude_code` selected and `claude` not on PATH → rc 2, `claude binary not found; install Claude Code or choose another provider`.
- `openai_compatible` with no base URL → rc 2 before any call.
- a key present in both env and `user_config_dir()/env` → env wins; `doctor` says which source.

### Failure scenarios
- the provider answers 401 mid-review → the review stops, `errors[]` carries the provider message, the ledger row has `error`, partial reviews are persisted with their status.
- the ledger is unwritable → the call still returns; `usage_ledger.write_failed` is logged once per process, never swallowed silently.

## Architecture Impact
- Adds dependency: SP-UNIVERSAL-PROVIDERS -> SP-UNIVERSAL-INSTALL (credential file under `user_config_dir()`; ledger under the project data dir)
- Adds dependency: SP-UNIVERSAL-PROVIDERS -> SP-UNIVERSAL-PROJECT-CONFIG (`[provider]` table)
- Adds dependency: SP-UNIVERSAL-PROVIDERS -> SP-BYO-PROVIDERS (the gateway-side descriptors stay the `gateway` provider's path; model-id defaults are shared)
- Adds dependency: SP-UNIVERSAL-PROVIDERS -> SP-CHAINS-SINGLE-SOURCE (`gateway` keeps reading `chains.env`; the direct providers never do)
- New shared state: `db:table:usage_ledger`, owned here, read by `repoach usage` and by the Yoob W1 report (`select sum(input_tokens+output_tokens)`).
- The agent loop's constructor changes from `base_url`/`api_key` to a `Provider`; the gateway client becomes one implementation.

## Diagram
```mermaid
flowchart TD
    A[AgentLoop] --> B[Provider protocol]
    B --> C[AnthropicProvider]
    B --> D[ClaudeCodeProvider]
    B --> E[OpenAICompatibleProvider]
    B --> F[GatewayProvider: chains.env]
    B --> G[usage_ledger]
```

## Acceptance Criteria
- [ ] AC1: `env -i HOME=$HOME PATH=$PATH repoach review pr 1 --dry-run` in any clone → rc 2, stderr contains `no provider configured` and the four names; `ss -ltnp | grep -c 8084` unchanged.
- [ ] AC2: `REPOACH_PROVIDER=anthropic REPOACH_ANTHROPIC_API_KEY=$KEY repoach review pr <n> --dry-run` with the gateway unit stopped → rc 0 or 2 (never 1), JSON `len(reviews)==4`; `sqlite3 <db> "select count(*) from usage_ledger where provider='anthropic'"` ≥ 5.
- [ ] AC3: `REPOACH_PROVIDER=claude_code` with `CLAUDE_CODE_OAUTH_TOKEN` set and no interactive login → same shape; `REPOACH_PROVIDER=claude_code` with no `claude` on PATH → rc 2, `claude binary not found`.
- [ ] AC4: `REPOACH_PROVIDER=gateway REPOACH_LLM_PROXY_BASE_URL=http://127.0.0.1:8084 repoach review pr <n> --dry-run` in repoach's own checkout → identical JSON shape to the pre-change run (fixture), and `pytest -q tests/unit` green.
- [ ] AC5: `pytest -q tests/unit/test_provider_select.py` → green: precedence flag > env > file; `claude_code` never selected by default; each provider's missing credential names the variable.
- [ ] AC6: `pytest -q tests/unit/test_usage_ledger.py` → green with a truthful `httpx.MockTransport` fake for the Anthropic SDK: one row per call, `cost_usd` null, `error` set on a 401.
- [ ] AC7: `repoach usage --json | jq '.totals.calls'` after AC2 ≥ 5; `repoach usage` prints `cannot say` in the cost column when every row has null cost.

## Open Questions
- Q1: gateway as an optional extra (`repoach[gateway]`) — umbrella Q5.
