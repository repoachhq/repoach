# SP-PROXY-HEALTH-BREAKER — close the open health loop

**Status:** specified
**Redesign slice:** D — pillar 1. Umbrella:
`docs/proxy_routing_redesign_architecture.md`. Builds on the completed
pillar 2 (A/B/C0/C1/C, #405–#409).
**Touches forbidden paths:** no.

## Why

The original diagnosis of this arc: the chain is static and health is an
**open loop**. A dead (410) or cold (ReadTimeout) model stays at the
chain head and is re-tried first on every request; failures are logged
but never fed back into routing. The 2026-06-19 session-start probe
proved it live (sonnet ReadTimeout + coder 410, both stuck at their
heads).

Pillar 2 made `Chain` first-class, so closing the loop is now small: an
in-process breaker that the live failover loop trips on failure and the
router consults when building the next chain.

## Change

### `routing/breaker.py` (new)

`BreakerState` — a clock-free map `ModelRef -> down_until`:
- `trip(ref, *, now, ttl_s)` — mark down until `now + ttl_s` (extends,
  never shortens, an existing trip).
- `recover(ref)` — clear immediately (it just served content).
- `down_refs(now) -> frozenset[ModelRef]` — currently-down refs, pruning
  any whose TTL has lapsed (so a recovered model re-enters automatically).
- `clear()` — reset (test hermeticity).

A process-level singleton (`get_breaker()`) so state survives across
requests; `reset_breaker()` clears it. Timestamps (`time.monotonic()`)
are passed in by callers, keeping the state testable.

### `api/model_router.py`

`resolve_chain` filters the breaker's down refs together with
`skip_models` before resolving: `chain.without(down_refs(now) |
skip_refs)`. `Chain.without` already guarantees non-empty (a fully-down
chain still yields its head — loud failure beats an empty chain).

### `api/services.py`

In `_stream_with_failover`: on both failover-fire sites (the transport
exception path and the empty-completion path) `get_breaker().trip(...)`
the candidate's ref with `settings.breaker_ttl_s` (gated by
`settings.breaker_enabled`); on the success path `recover()` the served
ref (clears a fallback-head that came back). Tripping mid-walk does not
disturb the already-built chain — it shapes the *next* request.

### `config/settings.py`

`breaker_enabled: bool = True`, `breaker_ttl_s: float = 120.0`, with
`_LEGACY_TO_FEROVA_ALIAS` entries (`BREAKER_ENABLED`, `BREAKER_TTL_S`).

### `tests/unit/conftest.py`

Autouse `reset_breaker()` before each test — the singleton is shared
state; without the reset a trip in one test would leak into the next.

## Acceptance

- New `tests/unit/test_health_breaker.py`:
  - `BreakerState` trip / down_refs / recover / TTL expiry / prune (with
    injected `now`);
  - `ModelRouter.resolve_chain` excludes a tripped ref; an all-down chain
    falls back to its head;
  - integration: a transport failover trips the failed ref (it is
    `is_down` after the walk); a recovered head clears on success.
- Existing failover suites stay green under the autouse reset.
- `test_settings_sharp_prefix_aliases` covers the two new keys.
- Full `pytest tests/unit` green; ruff + format clean; no inline
  comments; no silent except.

## Follow-on

- `SP-PROXY-BREAKER-PROBE-SEED` (optional D2): seed the breaker at
  startup from recent `nim_health_probe` degraded rows, so a model known
  cold from the 15-min probe is skipped before the first live failure.
- Per-reason TTL (a 410 EOL deserves a longer cool-down than a transient
  timeout) — a refinement over the single `breaker_ttl_s`.
