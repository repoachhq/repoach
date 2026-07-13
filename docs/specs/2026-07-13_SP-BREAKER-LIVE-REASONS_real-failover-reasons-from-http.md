---
id: SP-BREAKER-LIVE-REASONS
title: Propagate real upstream status to the failover classifier and atomize breaker escalation
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

# Propagate real upstream status to the failover classifier and atomize breaker escalation

## Intent

On live traffic every provider failure is classified as `empty_completion` and
tripped with the short transient TTL, so first-occurrence dead-hop quarantine
(SP-CHAIN-DEAD-HOP-QUARANTINE) never fires — a 402 dead hop is retried every
120s instead of quarantined for 6h. Carry the real upstream status/reason from
the HTTP transports to the failover classifier so QUARANTINE/TERMINAL reasons
match live faults, and move the escalation-count logic inside the breaker.

## Context

`src/ferova/llm_proxy/providers/openai_compat.py:342` and
`src/ferova/llm_proxy/providers/anthropic_messages.py:251` both catch EVERY
upstream exception inside the streaming generator and convert it to an SSE error
event (`sse.emit_error` / `_emit_error_events`) instead of raising. The failover
dispatcher `_stream_with_failover` (`services.py:223`) peeks the stream
(`peek_for_content`, `services.py:266`); because the transport already swallowed
the exception, the `except Exception` at `services.py:267` never sees a
401/402/429 — the stream simply yields no content. Every such case therefore
falls through to the `empty_completion` branch (`services.py:322,336`) and trips
the breaker with the transient TTL.

`_classify_failover_reason` (`services.py:39-77`) has the full vocabulary
(`auth_failed`, `provider_401..410`, `provider_5xx`, `rate_limited`) — but it is
DEAD for NIM/OpenRouter because the exception never reaches it. `ttl_for_reason`
would map `provider_402` to the quarantine TTL, but it is never invoked with
that reason on live traffic.

M21: `services.py:205` `_trip_breaker` reads the breaker's private
`breaker._consecutive_failures` and does a non-atomic peek-then-trip
(`services.py:205-213`) to compute the escalated TTL — a concurrency hazard and
an encapsulation break. Audit 2026-07-13 findings H10 + M21.

## Goals

- G1: the HTTP transports carry the real upstream status/reason to the failover
  layer. Either (a) attach the upstream HTTP status code onto the SSE error path
  in a structured field the peek result exposes, or (b) raise a typed error
  (carrying `status_code`) that `_stream_with_failover` inspects for the
  content-less-but-faulted case. The dispatcher must be able to distinguish a
  genuine empty completion from a 401/402/429/5xx.
- G2: `_classify_failover_reason` receives the real status so a 402 yields
  `provider_402`, a 401 `auth_failed`/`provider_401`, a 429 `rate_limited` — and
  `ttl_for_reason` selects the QUARANTINE/TERMINAL TTL, so a dead hop is
  quarantined on first occurrence (SP-CHAIN-DEAD-HOP-QUARANTINE fires live).
- G3: `empty_completion` is reserved for a truly content-less, non-errored
  stream — not the catch-all for swallowed HTTP faults.
- G4: escalation-count logic moves INSIDE the breaker: `_trip_breaker` stops
  reading `breaker._consecutive_failures`; the breaker exposes an atomic
  trip-and-escalate method that computes the effective TTL from its own state
  under its own lock, eliminating the peek-then-trip race.

## Non-Goals

- NG1: no change to the SSE wire format seen by well-behaved clients on the
  success path.
- NG2: no change to the quarantine TTL values or the
  SP-CHAIN-DEAD-HOP-QUARANTINE reason->TTL table itself.
- NG3: no new provider — only the two existing HTTP transports and the breaker.

## Assumptions

- A1: the upstream HTTP status is available at the point each transport catches
  the error (the response object or the raised provider error carries it).
- A2: `ttl_for_reason` already maps `provider_402`/`auth_failed`/terminal
  reasons to quarantine/terminal TTLs (SP-CHAIN-DEAD-HOP-QUARANTINE) — this spec
  only ensures those reasons actually arrive.

## Interface

Changed (in-place, no new module):
- Breaker gains an atomic escalation entry point, e.g.
  `trip_escalating(ref: ModelRef, *, now: float, base_ttl_s: float,
  quarantine_ttl_s: float, threshold: int, reason: str) -> int` returning the
  post-trip consecutive-failure count, computing the escalated TTL from internal
  state under the breaker lock. `_trip_breaker` calls this instead of reading
  `_consecutive_failures` and calling `escalated_ttl` + `trip` separately.
- The peek/transport boundary gains a way to surface the upstream status. Prefer
  a typed `UpstreamStatusError(status_code: int, reason: str)` raised by the
  transports (or carried on `PeekResult`) so `_stream_with_failover` classifies
  it via `_classify_failover_reason` before falling back to `empty_completion`.

## Behavior

### Nominal

Content is served; the breaker records recovery (`services.py:291`) unchanged.

### Edge cases

- Upstream 402 (credits exhausted) -> failover reason `provider_402` ->
  quarantine TTL on first occurrence.
- Upstream 401 -> `auth_failed`/`provider_401` -> terminal/quarantine TTL.
- Upstream 429 -> `rate_limited`.
- Genuinely empty stream with HTTP 200 and no error -> `empty_completion`
  (transient TTL) as before.

### Failure scenarios

- If the status cannot be recovered for a given fault, the reason falls back to
  the existing classifier default (`exception:<Type>` / `empty_completion`) —
  never worse than today. Fail CLOSED for dead hops: any recovered 4xx that is a
  dead-hop reason quarantines rather than short-cycles.

## Architecture Impact

- Adds dependency: none new at the spec-graph level — in-place modification of
  `services.py`, `openai_compat.py`, `anthropic_messages.py`, and the breaker
  module (all owned by existing specs). No new cross-owner import; the typed
  error, if added, lives in the provider/exceptions module already imported by
  the transports.
- New / changed coupling, cycles, or shared state: the breaker's escalation
  state stops being read externally — coupling DECREASES (encapsulation
  restored). No cycle.

## Diagram

```mermaid
flowchart LR
  UP[Upstream 402/401/429] --> TX[HTTP transport]
  TX -->|typed status error| PK[peek_for_content]
  PK --> DP[_stream_with_failover]
  DP --> CL[_classify_failover_reason]
  CL --> BR[breaker.trip_escalating]
  BR --> Q[quarantine TTL]
```

## Acceptance Criteria

- [ ] AC1: unit — `_classify_failover_reason` returns `provider_402` for an
  upstream-402-carrying error, `auth_failed` for 401, `rate_limited` for 429;
  the breaker's `trip_escalating` computes the escalated TTL atomically and
  returns the incremented count (no external read of `_consecutive_failures`).
- [ ] AC2 (INTEGRATION): drive the real failover path — a provider backed by an
  `httpx.MockTransport` returning HTTP 402 (truthful boundary fake), wired
  through `_stream_with_failover` on a one-candidate chain; assert the breaker
  is tripped with reason `provider_402` and the quarantine TTL (assert the
  recorded reason and TTL on the real breaker), NOT `empty_completion` with the
  transient TTL. No monkeypatching of Ferova code — only the HTTP transport is
  faked.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_proxy_failover_live_reasons.py::test_upstream_402_quarantines`
  and `::test_upstream_401_and_429_reasons`;
  `tests/unit/test_health_breaker.py::test_trip_escalating_is_atomic`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
