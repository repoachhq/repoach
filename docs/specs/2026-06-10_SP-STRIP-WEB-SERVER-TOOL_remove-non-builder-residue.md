# SP-STRIP-WEB-SERVER-TOOL — remove the dead web-server-tool path (non-builder residue)

## Metadata

- **Status**: OPEN
- **Priority**: P2
- **Owner**: operator
- **Executor**: hand-implemented
- **Opened**: 2026-06-10

## Why

The repo is the review factory (the builder) **only** — "today we build
the Builder; everything outside it is removed" (operator, 2026-06-10).
The Anthropic **web-server-tool** path (`web_search` / `web_fetch`) is
non-builder residue from the stripped messaging/web era:

- **No current caller uses it.** The builder's agents call tools
  `Read` / `Grep` / `Glob` / `Write`, never `web_search` / `web_fetch`
  (grep over `src/` finds no producer of such requests).
- It is also a **particularisation** in the NIM-access entry point:
  `ClaudeProxyService.create_message` short-circuits web-server-tool
  requests to `stream_web_server_tool_response`, bypassing the one
  universal chain (SP-PROXY-UNIVERSAL-CHAIN). It is the **last** branch
  in `create_message` other than the chain walk.

Audit confirmed the boundary: the capability gateway (`/v1/agent` +
`agent_dispatcher` + `llm/capability`) IS builder infrastructure — the
reviewers / Planner / Developer reach the LLM through
`agent_engine.AgentLoop → /v1/agent`. It is **kept**. Only the
web-server-tool is removed.

## What

1. **`api/services.py`** — delete the `is_web_server_tool_request`
   short-circuit and its import. `create_message` then always returns a
   chain-walked `StreamingResponse` (one path).
2. **`api/web_server_tools.py`** — delete the module (319 lines: HTML
   parsers, `_run_web_search`, `stream_web_server_tool_response`, …).
3. **`api/agent_dispatcher.py`** — remove the now-dead
   non-`StreamingResponse` branch and `_passthrough_optimized_response`
   (the web-server-tool short-circuit was the only non-streaming result
   `create_message` could return; it always streams now). The shared
   imports (`AgentResponse`, `TextBlock`, `TraceEntry`, `Usage`) stay —
   the main dispatch function uses them.

## Files in scope

- `src/ferova/llm_proxy/api/services.py`
- `src/ferova/llm_proxy/api/web_server_tools.py` (deleted)
- `src/ferova/llm_proxy/api/agent_dispatcher.py`

## Out of scope

- The capability gateway (`/v1/agent`, `agent_dispatcher.dispatch_agent_request`,
  `llm/capability.py`) — builder infrastructure, kept.
- The generic Anthropic `Tool` / content-block models (they parse any
  tool shape; no web-specific code lives there).

## Definition of Done

- `create_message` has a single path — no web-server-tool branch.
- `web_server_tools.py` no longer exists; grep for
  `web_server_tool` / `stream_web_server` / `is_web_server` is clean.
- `agent_dispatcher` treats `create_message`'s result as a
  `StreamingResponse` unconditionally (passthrough gone).
- `ruff` + `ruff format --check` + full `pytest tests/unit` green; zero
  inline comments; no `# noqa`.

## Commit plan

1. `chore(proxy): remove the dead web-server-tool path (non-builder residue)`

## Risks

- **A future capability may want web search** (external data / docs).
  Accepted: it is dead today; re-add it as a proper tool when that need
  exists, rather than carry an unused 319-line branch now.
