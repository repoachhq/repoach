---
id: SP-PROVIDER-INIT-DEDUP
title: Fold duplicated transport-init boilerplate into shared BaseProvider helpers
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: [src/repoach/llm_proxy/providers/base.py, src/repoach/llm_proxy/providers/anthropic_messages.py]
  resources: N/A

depends_on: [SP-USAGE-REASONING-SPLIT]
provides_to: []

constraints: {}
---

# Fold duplicated transport-init boilerplate into shared BaseProvider helpers

## Intent

`OpenAIChatTransport.__init__` and `AnthropicMessagesTransport.__init__`
each independently build the same scoped rate limiter and the same
`httpx.Timeout` from `ProviderConfig` fields. A future change to how
timeouts or rate-limiter scoping derive from `ProviderConfig` (e.g. a new
pool-limit field) must currently be made in both places and can silently
drift. Lift the shared construction into two `BaseProvider` helpers so
each transport keeps only the genuinely different client-construction
line.

## Context

- `src/repoach/llm_proxy/providers/openai_compat.py:90-118`
  (`OpenAIChatTransport.__init__`): builds
  `self._global_rate_limiter = GlobalRateLimiter.get_scoped_instance(provider_name.lower(), rate_limit=config.rate_limit, rate_window=config.rate_window, max_concurrency=config.max_concurrency)`,
  then constructs `httpx.Timeout(config.http_read_timeout, connect=config.http_connect_timeout, read=config.http_read_timeout, write=config.http_write_timeout)`
  TWICE with identical field mapping — once for the optional proxied
  `httpx.AsyncClient` passed as `AsyncOpenAI(..., http_client=...)`, once
  for `AsyncOpenAI`'s own `timeout=` kwarg.
- `src/repoach/llm_proxy/providers/anthropic_messages.py:36-50`
  (`AnthropicMessagesTransport.__init__`): builds the identical
  `GlobalRateLimiter.get_scoped_instance(...)` call and the identical
  `httpx.Timeout(...)` field mapping, written independently.
- `src/repoach/llm_proxy/providers/base.py:30-84` (`BaseProvider`):
  currently holds only `self._config` and `_is_thinking_enabled`; no
  shared helper for timeout or rate-limiter construction exists, so both
  subclasses hand-roll it.
- Confirmed still real against the current `develop` tree on 2026-07-24
  re-verification: line ranges and duplicated bodies above match
  byte-for-byte.
- Ownership: no spec's frontmatter lists `base.py` or
  `anthropic_messages.py` under `owns.code` (both are currently
  unowned — `SP-PROXY-UNIVERSAL-CHAIN` predates the frontmatter
  ownership convention and mentions `base.py` only in prose;
  `SP-PROXY-FIRST-BYTE-DEADLINE` owns `_failover.py` +
  `config/settings.py`, not `base.py`; `SP-BREAKER-LIVE-REASONS`
  mentions `anthropic_messages.py` in prose but ships
  `owns.code: []`). `openai_compat.py` IS owned by
  `SP-USAGE-REASONING-SPLIT` (`owns.code` lists it explicitly); this
  spec depends on that owner rather than re-claiming the file.

## Goals

- G1: `BaseProvider` exposes `_build_timeout() -> httpx.Timeout` reading
  `self._config.http_read_timeout` / `http_connect_timeout` /
  `http_write_timeout`, with exactly the field mapping currently
  duplicated in both transports.
- G2: `BaseProvider` exposes
  `_scoped_rate_limiter(provider_name: str) -> GlobalRateLimiter`
  wrapping
  `GlobalRateLimiter.get_scoped_instance(provider_name.lower(), rate_limit=self._config.rate_limit, rate_window=self._config.rate_window, max_concurrency=self._config.max_concurrency)`.
- G3: `OpenAIChatTransport.__init__` calls both helpers instead of
  hand-rolling the rate limiter and either `httpx.Timeout(...)`
  construction (the plain one and the proxied-`http_client` one) — same
  resulting values, no behavior change.
- G4: `AnthropicMessagesTransport.__init__` calls both helpers instead of
  hand-rolling the rate limiter and timeout construction.
- G5: the genuinely different client-construction line in each subclass
  (`AsyncOpenAI(...)` vs `httpx.AsyncClient(...)`) is untouched beyond
  swapping in the helper-built timeout.

## Non-Goals

- NG1: no behavior change beyond removing the duplication — timeout
  values, the rate-limiter scoping key (`provider_name.lower()`), and
  `max_concurrency` semantics are byte-for-byte identical to today.
- NG2: no change to `cleanup()`, the streaming loops, or error mapping
  in either transport.
- NG3: no change to `ProviderConfig` fields or defaults
  (`base.py:10-27`).
- NG4: no change to `GlobalRateLimiter` itself (`rate_limit.py`) — only
  its call site moves.
- NG5: no touch to the optional proxied `httpx.AsyncClient(...)`
  branch's `proxy=` kwarg or any OpenAI-SDK-specific `http_client=`
  wiring — only the timeout construction inside that branch is
  delegated to `_build_timeout()`.

## Interface

`src/repoach/llm_proxy/providers/base.py` (`BaseProvider`):

```python
def _build_timeout(self) -> httpx.Timeout:
    """Build the httpx.Timeout derived from this provider's ProviderConfig.

    Maps ``http_read_timeout`` to both the overall timeout and the
    ``read`` timeout, matching every transport's existing timeout shape.
    """

def _scoped_rate_limiter(self, provider_name: str) -> GlobalRateLimiter:
    """Return the process-wide rate limiter scoped to ``provider_name``.

    Delegates to ``GlobalRateLimiter.get_scoped_instance`` with the
    scope lower-cased and the concurrency/window/limit fields read
    from this provider's ``ProviderConfig``.
    """
```

`src/repoach/llm_proxy/providers/openai_compat.py`
(`OpenAIChatTransport.__init__`):
- `self._global_rate_limiter = self._scoped_rate_limiter(provider_name)`
- both `httpx.Timeout(...)` call sites replaced with
  `self._build_timeout()`.

`src/repoach/llm_proxy/providers/anthropic_messages.py`
(`AnthropicMessagesTransport.__init__`):
- `self._global_rate_limiter = self._scoped_rate_limiter(provider_name)`
- the `httpx.Timeout(...)` call site replaced with
  `self._build_timeout()`.

## Behavior

### Nominal

- Constructing either transport with a `ProviderConfig` produces the
  same `httpx.Timeout` (same read/connect/write/overall values) and the
  same scoped `GlobalRateLimiter` instance (same scope key, same
  `rate_limit`/`rate_window`/`max_concurrency`) as before this spec.

### Edge cases

- `config.proxy` set (OpenAI-compatible path only): the proxied
  `http_client`'s timeout is built by the same `_build_timeout()` call as
  the SDK's own `timeout=`, so both stay in sync automatically instead of
  drifting if only one call site were fixed by hand in the future.
- `provider_name` with mixed case: `_scoped_rate_limiter` lower-cases it
  exactly once (matching current behavior), so `"NIM"` and `"nim"`
  continue to scope to the same limiter.

### Failure scenarios

- N/A — pure refactor, no new failure path. `GlobalRateLimiter.get_scoped_instance`
  still raises `ValueError` on an empty scope, unchanged.

## Acceptance Criteria

- [ ] AC1: unit — `BaseProvider._build_timeout()` returns an
  `httpx.Timeout` whose `.read`, `.connect`, `.write` attributes equal
  the `ProviderConfig`'s `http_read_timeout`, `http_connect_timeout`,
  `http_write_timeout` respectively (construct a minimal concrete
  `BaseProvider` subclass in the test, since `BaseProvider` is abstract).
- [ ] AC2: unit — `BaseProvider._scoped_rate_limiter("NIM")` returns the
  same limiter instance as
  `GlobalRateLimiter.get_scoped_instance("nim", rate_limit=..., rate_window=..., max_concurrency=...)`
  called directly with the same config fields (assert identity,
  confirming the lower-casing and field mapping).
- [ ] AC2b (INTEGRATION): construct a real `OpenAIChatTransport` subclass
  instance and a real `AnthropicMessagesTransport` subclass instance from
  equivalent `ProviderConfig` objects; wrap `BaseProvider._build_timeout`
  and `BaseProvider._scoped_rate_limiter` with
  `unittest.mock.patch.object(..., wraps=BaseProvider._build_timeout)` /
  `wraps=BaseProvider._scoped_rate_limiter` before constructing either
  transport, and assert both spies were called at least once by each
  transport's `__init__` — this is the test that FAILS on pre-change
  code, where each transport hand-rolls its own construction inline with
  no shared call path to observe (the spy records zero calls pre-change).
- [ ] AC3: promised tests —
  `tests/unit/test_provider_base_init_helpers.py::test_build_timeout_maps_config_fields`,
  `::test_scoped_rate_limiter_matches_direct_call`, and
  `::test_both_transports_route_init_through_base_helpers`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green;
  zero inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `repoach arch graph --check` exits 0.

## Architecture Impact

- Adds/Removes dependency: none — `base.py` gains an import of `httpx`
  and `GlobalRateLimiter` from `providers/rate_limit.py` (no cycle:
  `rate_limit.py` imports neither `base.py` nor any provider transport
  module — confirmed by inspection, its only imports are `asyncio`,
  `random`, `time`, `collections`, `contextlib`, `typing`, `openai`,
  `loguru`). `openai_compat.py` and `anthropic_messages.py` lose no
  imports (both already need `httpx` for their own client construction).
- New / changed coupling, cycles, or shared state: reduces coupling —
  two independently duplicated constructions collapse into one shared
  implementation in `BaseProvider`. `openai_compat.py` (owned by
  `SP-USAGE-REASONING-SPLIT`) is a dependency, not an owned file, of this
  spec.

## Diagram

N/A (in-place refactor within the existing `providers` package; no new
module boundary).

## Open Questions

(none)
