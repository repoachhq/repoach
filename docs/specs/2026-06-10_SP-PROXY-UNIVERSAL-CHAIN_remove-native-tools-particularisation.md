# SP-PROXY-UNIVERSAL-CHAIN — one universal chain for every request (drop the native-tools particularisation)

## Metadata

- **Status**: OPEN
- **Priority**: P1
- **Owner**: operator
- **Executor**: hand-implemented (wide cross-file removal + test surgery)
- **Opened**: 2026-06-10

## Why

Every request must walk the **same** chain — no special-casing. Today
there is one particularisation: `resolve_chain(require_native_tools=…)`.
When a request carries `tools=[…]` (the builder's Planner/Developer
always do), the chain is **filtered to native-tool providers**, which
**drops `claude_code`** and falls back to `[chain[0]]`. Consequences:

- **The builder loses its backstop on tool requests.** When the
  native-tool providers (NIM, OpenRouter) are all down, the filtered
  chain has no `claude_code` entry → the chain exhausts → **502**, even
  though `claude_code`/Max is up.
- It is a per-request branch ("tools" vs "tools-less") that the operator
  wants gone.

The filter's original rationale — *"`claude_code` can't do native
tools"* — is **obsolete**: SP-CC-EMUL-HARDEN (#320) made the
`claude_code` provider fully **emulate** tools (`_build_prompt` →
`_render_tools_appendix` injects the tool contract + the
`<tool_use>{…}</tool_use>` format into the system prompt;
`HeuristicToolParser` converts the model's text back into native
`tool_use` SSE blocks; validated live). So `claude_code` CAN serve a
tools request — there is no reason to filter it out, and every reason to
keep it as the last-resort backstop.

## What

Remove the native-tools particularisation entirely. Every request — with
or without tools — walks the **one** configured chain
(`NIM → OpenRouter → claude_code`). The failover prefers the native
providers (they are first in the chain); `claude_code` is reached only
as the backstop and serves tools via emulation. No happy-path change
(NIM still answers first when warm).

1. **`api/model_router.py`** — drop `require_native_tools` from
   `resolve`, `_select_chain_entry`, `resolve_chain`,
   `resolve_messages_request`; delete the native-tool filter and the
   `TOOLS_AWARE_DISPATCH` logging; drop the
   `provider_supports_native_tools` import. `_select_chain_entry`
   becomes "the head of the chain"; `resolve_chain` returns the full
   chain (minus `skip_models`).
2. **`api/services.py`** — `create_message` no longer computes
   `request_has_tools` nor passes `require_native_tools`; the
   `CHAIN_RESOLVED` log drops the `require_tools` field; docstring
   updated.
3. **`providers/registry.py`** — delete `_PROVIDERS_WITHOUT_NATIVE_TOOLS`
   and `provider_supports_native_tools`.
4. **`providers/base.py`** — delete the `SUPPORTS_NATIVE_TOOLS` class
   attribute + its docstring paragraph.
5. **`providers/claude_code/client.py`** — delete
   `SUPPORTS_NATIVE_TOOLS = False`; rewrite the docstring (it is now a
   first-class chain member that emulates tools, no longer "skipped by
   the filter"). **Keep the emulation untouched.**

## Files in scope

- `src/ferova/llm_proxy/api/model_router.py`
- `src/ferova/llm_proxy/api/services.py`
- `src/ferova/llm_proxy/providers/registry.py`
- `src/ferova/llm_proxy/providers/base.py`
- `src/ferova/llm_proxy/providers/claude_code/client.py`
- `tests/unit/test_proxy_failover_toolless.py` (the toolless-vs-toolsy
  comparison now expects identical full chains)
- `tests/unit/test_proxy_tools_aware_dispatch.py` (the filter is gone —
  repurpose to assert the universal chain, or remove)
- `tests/unit/test_proxy_chain_failover.py` (drop/rewrite the two
  `resolve_chain` filter tests)

## Out of scope

- The `claude_code` tool emulation itself (kept verbatim).
- `skip_models` semantic failover (a per-request dynamic skip, applies
  universally — not a particularisation).
- The capability-tier chain selection (opus/sonnet/haiku/coder — that is
  which chain, not a path branch).
- The web-server-tool short-circuit (orthogonal).

## Smoke scenario

`resolve_chain("claude-sonnet-4-6")` returns the **same** full chain
whether or not the request carries tools, ending in `claude_code`.
Driving `_stream_with_failover` with a tools request where every NIM/OR
candidate empties, the chain still reaches `claude_code` (the backstop)
instead of exhausting into a 502.

## Definition of Done

- `resolve_chain` has no `require_native_tools` parameter and returns the
  full configured chain (minus `skip_models`) for any request —
  `test_proxy_failover_toolless.py` (toolless and former-toolsy chains
  are now identical and both include `claude_code`).
- A request whose NIM/OR candidates all empty still reaches `claude_code`
  as the backstop (no 502) — service-level test in
  `test_proxy_chain_failover.py`.
- `provider_supports_native_tools` / `_PROVIDERS_WITHOUT_NATIVE_TOOLS` /
  `SUPPORTS_NATIVE_TOOLS` no longer exist (grep is clean).
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `feat(proxy): one universal chain — drop the native-tools filter (claude_code emulates)`
2. `refactor(providers): remove the native-tools registry concept`
3. `test(proxy): universal chain for tools + tools-less; claude_code backstop`

## Risks

- **Emulated tools slightly less reliable than native.** Mitigated: NIM
  + OpenRouter (native) are tried FIRST; `claude_code` (emulated) is the
  last-resort backstop only when they are all down — a possibly-imperfect
  answer beats a hard 502.
- **Latency on the backstop.** `claude -p` is slower than NIM, but only
  on the degraded path where NIM already failed.
