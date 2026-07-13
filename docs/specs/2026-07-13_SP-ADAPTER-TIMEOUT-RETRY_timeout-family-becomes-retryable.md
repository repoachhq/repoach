---
id: SP-ADAPTER-TIMEOUT-RETRY
title: Map the whole httpx timeout family to the retryable gateway error
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

# Map the whole httpx timeout family to the retryable gateway error

## Intent

A connect stall or pool/write timeout against the local proxy currently crashes
a whole multi-turn agent session instead of being retried. The gateway adapter
catches only two of httpx's transport exceptions; broaden the catch so every
timeout and transport fault becomes the retryable `GatewayTransportError`.

## Context

`src/ferova/agent_engine/adapters.py:165-174` wraps the proxy POST in
`except (httpx.ConnectError, httpx.ReadTimeout)` and re-raises as
`GatewayTransportError` (`adapters.py:42`). That class is the ONLY transport
class the agent loop retries: `AgentLoop._call_turn_with_retry`
(`agent_loop.py:488`) catches `(GatewayTransportError, GatewayChainExhausted)`
at `agent_loop.py:546` and backs off; anything else propagates and aborts.

The client is built with `httpx.Timeout(self._timeout_s, connect=5.0)`
(`adapters.py:165`). In httpx's hierarchy `ConnectTimeout`, `PoolTimeout`,
`WriteTimeout`, and `ReadTimeout` all subclass `TimeoutException` — and
`ConnectTimeout` is NOT a subclass of `ConnectError`. So a 5s connect stall
raises `ConnectTimeout`, escapes the two-class catch, and kills the session;
`PoolTimeout`, `WriteTimeout`, and `RemoteProtocolError` escape identically.
Audit 2026-07-13 finding H9 (verified).

## Goals

- G1: the POST `except` in `adapters.py:165-174` catches the whole timeout
  family via `httpx.TimeoutException`, plus `httpx.TransportError` (the base for
  connection/pool/protocol faults) and `httpx.RemoteProtocolError`, mapping all
  to `GatewayTransportError`.
- G2: the mapped exceptions are retried by the unchanged
  `_call_turn_with_retry` backoff loop — no change needed there once they are
  wrapped.
- G3: genuinely non-retryable programming errors are NOT swallowed — only
  httpx transport/timeout types are caught (no bare `except Exception`).

## Non-Goals

- NG1: no change to retry counts, backoff schedule, or `connect=5.0`.
- NG2: no change to the 4xx/5xx status handling below the try block
  (`adapters.py:176-189`), which already raises `GatewayTransportError` for
  408/429/5xx.
- NG3: no new exception class.

## Assumptions

- A1: `httpx.TimeoutException` is the common base of `ConnectTimeout`,
  `ReadTimeout`, `WriteTimeout`, and `PoolTimeout`; `httpx.TransportError` is
  the base covering `ConnectError`, `RemoteProtocolError`, and pool/network
  transport faults. Catching both bases plus `RemoteProtocolError` explicitly
  (belt-and-braces) covers every case in the finding.
- A2: these faults are transient by nature — retrying is the correct policy.

## Interface

N/A (in-place fix, no signature change). `GatewayTransportError` is unchanged;
only the `except` tuple in `adapters.py` broadens.

## Behavior

### Nominal

A successful POST is unaffected.

### Edge cases

- `ConnectTimeout` (5s connect stall) -> `GatewayTransportError` -> retried.
- `PoolTimeout` / `WriteTimeout` / `ReadTimeout` -> `GatewayTransportError` ->
  retried.
- `RemoteProtocolError` (server closed connection mid-response) ->
  `GatewayTransportError` -> retried.

### Failure scenarios

- After the retry budget is exhausted, the loop re-raises the last
  `GatewayTransportError` (existing behavior at `agent_loop.py:556-557`) —
  the session still ends, but only after real retries, not on the first stall.
  Fail CLOSED remains: no fault is silently swallowed.

## Architecture Impact

- Adds dependency: none — in-place modification of `adapters.py` (owned by an
  existing spec); no new cross-owner import (httpx already imported there).
- New / changed coupling, cycles, or shared state: none.

## Diagram

N/A (in-place fix).

## Acceptance Criteria

- [ ] AC1: unit — with `ProxyGatewayClient` pointed at an
  `httpx.MockTransport` (truthful boundary fake) whose handler raises
  `httpx.ConnectTimeout`, `client.call(...)` raises `GatewayTransportError`
  (not `ConnectTimeout`); parametrize the same over `PoolTimeout`,
  `WriteTimeout`, `ReadTimeout`, and `RemoteProtocolError`.
- [ ] AC2 (INTEGRATION): drive the real retry loop — construct the actual
  `AgentLoop` over a `ProxyGatewayClient` backed by an `httpx.MockTransport`
  that raises `httpx.ConnectTimeout` on the first N calls then returns a valid
  agent response; assert the turn ultimately succeeds via
  `_call_turn_with_retry` (the timeout was treated as retryable, session not
  aborted). No monkeypatching of Ferova code — only the transport is a fake.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_agent_gateway_transport.py::test_connect_timeout_is_gateway_transport_error`
  (parametrized over the timeout family) and
  `::test_agent_loop_retries_connect_timeout`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
