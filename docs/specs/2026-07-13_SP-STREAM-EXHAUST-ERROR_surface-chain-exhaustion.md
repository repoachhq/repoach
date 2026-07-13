---
id: SP-STREAM-EXHAUST-ERROR
title: Surface all-providers-down as a terminal SSE error, not a silent truncation
version: 0.1
status: draft
author: jfaye (Ferova audit 2026-07-13)
created: 2026-07-13
updated: 2026-07-13

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Surface all-providers-down as a terminal SSE error, not a silent truncation

## Intent

When the whole chain is exhausted, the dispatcher raises `HTTPException(502)`
from inside a streaming body iterator — but Starlette has already sent the 200
headers, so the client sees a 200 with an empty/truncated SSE stream and never
the documented 502. Surface exhaustion as a well-formed terminal SSE error event
the client can unambiguously detect.

## Context

`src/ferova/llm_proxy/api/services.py:348-357`: `_stream_with_failover` is the
body iterator of a `StreamingResponse` (`create_message`, `services.py:95-100`).
When every candidate fails, it either `raise last_error` (`services.py:349`) or
`raise HTTPException(status_code=502, ...)` (`services.py:350-357`). Because
Starlette commits the 200 status and headers before it begins iterating the
body, raising mid-iteration aborts the response body AFTER a 200 is already on
the wire. External clients therefore observe HTTP 200 + a silent
empty/truncated SSE stream, never the 502 the code intends.

The dispatcher already emits a `proxy_chain_exhausted` LOG event
(`services.py:338-347`) and, on the empty-completion path, uses
`sse.emit_error` for per-attempt errors — the SSE builder can emit a terminal
error event. Audit 2026-07-13 finding M22.

## Goals

- G1: on chain exhaustion, the streamed response carries a well-formed TERMINAL
  SSE error event with an explicit, documented error type (e.g.
  `error` event with `type: "chain_exhausted"` / an `overloaded_error`-shaped
  Anthropic error body) so a client parsing the SSE stream can detect the
  failure deterministically — instead of a silent truncation.
- G2: where feasible, pre-flight the exhaustion condition so a real HTTP 502 can
  be returned BEFORE the `StreamingResponse` starts (headers not yet committed);
  if the first candidate has already started streaming (headers committed), fall
  back to the terminal SSE error event of G1.
- G3: the existing `proxy_chain_exhausted` log event is preserved for operator
  dashboards.

## Non-Goals

- NG1: no change to the per-attempt failover behavior or the breaker tripping.
- NG2: no attempt to retroactively change an already-sent 200 to a 502 — that is
  impossible once headers are committed; G1 is the honest remedy for that case.
- NG3: no new SSE framework — reuse the existing `SSEBuilder.emit_error` / error
  event shape.

## Assumptions

- A1: the SSE builder can emit a terminal error event after content blocks are
  closed (the empty-completion path already does `sse.emit_error`).
- A2: clients (the agent loop, reviewers) treat a terminal SSE error event as a
  failed turn — which the agent loop's `GatewayChainExhausted` mapping already
  expects; this spec makes that signal reliably present on the wire.

## Interface

Changed (in-place): `_stream_with_failover` replaces the terminal
`raise HTTPException(502)` (`services.py:350-357`) with yielding a terminal SSE
error event carrying an explicit error type; `create_message` optionally gains a
pre-flight so a not-yet-streaming exhaustion returns a real 502 Response.

## Behavior

### Nominal

Content is served by some candidate — unchanged.

### Edge cases

- All candidates fail before any content, headers already committed -> a
  terminal SSE `error` event with an explicit `chain_exhausted` type is yielded;
  the stream ends cleanly (no bare truncation).
- Exhaustion detectable before streaming begins -> HTTP 502 Response returned
  directly (G2).

### Failure scenarios

- `last_error` present -> the terminal SSE error carries its message (truncated,
  redacted) and the explicit type. Fail CLOSED: the client always receives an
  unambiguous terminal error rather than silence.

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `services.py` (owned by an existing spec) and reuse of the existing
  `SSEBuilder` error path. No new cross-owner import.
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — given a fully-failing one-candidate chain, the body iterator
  yields a terminal SSE error event whose parsed payload carries the explicit
  `chain_exhausted` (or equivalent) error type; assert no bare stream end
  without an error event.
- [ ] AC2 (INTEGRATION): drive the real endpoint via FastAPI `TestClient` with
  ALL providers failing (providers backed by `httpx.MockTransport` truthful
  boundary fakes returning failures); consume the streamed response and assert
  it carries the unambiguous terminal error event (not a silent/empty
  truncation), and — where pre-flight applies — that a 502 is returned before
  streaming. No monkeypatching of Ferova code.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_proxy_chain_exhaustion.py::test_exhaustion_emits_terminal_sse_error`
  and `::test_preflight_exhaustion_returns_502`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
