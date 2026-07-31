---
id: SP-CREDITS-CACHE-LOCK
title: Coalesce concurrent get_cached_credits callers behind an asyncio.Lock
version: 0.1
status: approved
author: agent
created: 2026-07-24
updated: 2026-07-24

owns:
  code: N/A
  resources: N/A

depends_on: [SP-CREDITS-CHECK]
provides_to: []

constraints: {}
---

# Coalesce concurrent get_cached_credits callers behind an asyncio.Lock

## Intent

`get_cached_credits` reads and later mutates three bare module-level
globals (`_cached_snapshot`, `_cached_fetched_at`, `_cached_is_failure`)
across an `await fetch_openrouter_credits(...)`, with no lock or
single-flight guard. Two concurrent callers inside the same process
that both observe an expired cache will both cross that `await` before
either write-back lands, so both issue a live OpenRouter request — a
classic cache-stampede window — against a credits endpoint the project
already tracks as budget-sensitive (OpenRouter balance already negative
per the llm-proxy-incident record). Add a module-level `asyncio.Lock`
with a double-checked-locking re-read so concurrent callers within one
process coalesce onto a single outstanding fetch.

## Context

- `src/repoach/health/credits.py:12-14` (confirmed present verbatim on
  `develop` at HEAD `bc4e4e0`, re-grepped 2026-07-24 — byte-identical,
  `git diff origin/develop -- src/repoach/health/credits.py` empty):

  ```python
  _cached_snapshot: CreditsSnapshot | None = None
  _cached_fetched_at: float | None = None
  _cached_is_failure: bool = False
  ```

  Three bare globals, no lock, no single-flight guard anywhere in the
  module.

- `src/repoach/health/credits.py:65-88`, `get_cached_credits` (unchanged
  line range confirmed):

  ```python
  async def get_cached_credits(
      api_key: str, *, client: httpx.AsyncClient, ttl_s: float, timeout_s: float = 3.0
  ) -> CreditsSnapshot | None:
      global _cached_snapshot, _cached_fetched_at, _cached_is_failure
      now = time.monotonic()
      if _cached_fetched_at is not None:
          if _cached_is_failure and (now - _cached_fetched_at) < min(60.0, ttl_s):
              return None
          if (
              not _cached_is_failure
              and _cached_snapshot is not None
              and (now - _cached_fetched_at) < ttl_s
          ):
              return _cached_snapshot
      result = await fetch_openrouter_credits(api_key, client=client, timeout_s=timeout_s)
      _cached_fetched_at = now
      if result is None:
          _cached_is_failure = True
          _cached_snapshot = None
          return None
      _cached_is_failure = False
      _cached_snapshot = result
      return result
  ```

  Two concurrent coroutines that both enter this function while
  `_cached_fetched_at` is expired both fall through the cache-hit
  checks (neither has yielded to the event loop yet), then both `await
  fetch_openrouter_credits(...)` — the first genuine yield point — so
  both requests actually fire before either write-back at line 81
  runs.

- Real concurrent call sites, all inside the single-process llm_proxy
  FastAPI app where multiple in-flight requests can race through this
  code path:
  - `src/repoach/llm_proxy/api/model_router.py:74`
  - `src/repoach/llm_proxy/api/routes.py:204`
  - `src/repoach/llm_proxy/routing/chain_regen.py:238`

- `src/repoach/health/credits.py` is owned by `SP-CREDITS-CHECK`
  (`docs/specs/2026-07-11_SP-CREDITS-CHECK_openrouter-credits-floor.md`);
  this spec's `depends_on: [SP-CREDITS-CHECK]` is the edge that
  authorizes editing it, exactly as `SP-NIM-PROBE-UNPARSEABLE-DIAG`'s
  `depends_on: [SP-NIM-CHAIN-HEALTH]` edited a file it did not own. No
  new module is introduced, so `owns.code: N/A`. Three other specs
  (`SP-CHAIN-STATUS-DIGEST`, `SP-PROXY-STATE-PERSIST`,
  `SP-BREAKER-PROVIDER-SCOPE`) reference `credits.py` in their own
  Context sections but only call its public functions — none of them
  list it under `owns.code`, so there is no second ownership conflict
  to reconcile.

- `src/repoach/llm_proxy/providers/rate_limit.py:51`
  (`self._lock = asyncio.Lock()`) is the existing in-repo precedent for
  an `asyncio.Lock` guarding shared async state, confirming the pattern
  is already idiomatic in this codebase (different subtree, not
  reused directly — `credits.py` has no class to attach the lock to).

## Goals

- G1: two or more concurrent callers of `get_cached_credits` that both
  observe an expired (or never-populated) cache issue at most ONE live
  call to `fetch_openrouter_credits` between them; every other
  concurrent caller receives the freshly-populated cache value once the
  first caller's fetch completes, rather than issuing its own request.
- G2: the coalescing is implemented via a module-level `asyncio.Lock`
  guarding the fetch-and-store section, with a double-checked re-read
  of the cache immediately after acquiring the lock — a caller that
  blocked on the lock while another was fetching must return the
  now-fresh cached value instead of triggering a redundant fetch.
- G3: the single-caller (uncontended) path is behaviorally identical to
  today — the initial cache-hit check stays outside the lock so the
  common case (fresh cache, no contention) pays no lock-acquisition
  cost beyond an uncontended `asyncio.Lock`, which is not measurably
  slower than today's lock-free read.

## Non-Goals

- NG1: no behavior change beyond adding the lock/coalescing path — TTL
  semantics (`ttl_s` for success, `min(60, ttl_s)` for cached failures),
  the returned `CreditsSnapshot` values, and `fetch_openrouter_credits`
  itself are byte-for-byte unchanged for the single-caller case.
- NG2: the pre-existing race where `reset_credits_cache()` writes the
  globals without taking the lock is left as-is — that function is a
  test/administrative reset, not a concurrent-request code path, and is
  out of scope here.
- NG3: no change to `fetch_openrouter_credits`'s "never raises"
  contract, its signature, or its HTTP call — this spec only changes
  how `get_cached_credits` sequences calls to it.
- NG4: no cross-process or cross-worker coalescing (e.g. via Redis or a
  file lock) — the fix is a single-process `asyncio.Lock`, matching the
  finding's proposed direction ("within one process").
- NG5: as an unavoidable side effect of G1/G2, the pre-existing race
  where two concurrent fetches could both write the globals
  (last-writer-wins, potentially leaving a stale snapshot from whichever
  request completed second) is also closed — this is not a second
  independent goal, it disappears automatically once only one fetch is
  ever in flight per stampede window, and is not separately tested
  beyond the AC1/AC2 coalescing assertions.

## Interface

`src/repoach/health/credits.py`:

- Add `import asyncio` to the stdlib import group (alphabetically before
  `import time`).
- Add a module-level `_cache_lock: asyncio.Lock = asyncio.Lock()`
  alongside the existing cache globals.
- `get_cached_credits`'s signature is unchanged:

  ```python
  async def get_cached_credits(
      api_key: str, *, client: httpx.AsyncClient, ttl_s: float, timeout_s: float = 3.0
  ) -> CreditsSnapshot | None:
  ```

  Its body changes (illustrative shape; no inline comments in the real
  diff):

  ```python
  def _cache_lookup(now: float, ttl_s: float) -> tuple[bool, CreditsSnapshot | None]:
      if _cached_fetched_at is None:
          return False, None
      if _cached_is_failure and (now - _cached_fetched_at) < min(60.0, ttl_s):
          return True, None
      if (
          not _cached_is_failure
          and _cached_snapshot is not None
          and (now - _cached_fetched_at) < ttl_s
      ):
          return True, _cached_snapshot
      return False, None


  async def get_cached_credits(
      api_key: str, *, client: httpx.AsyncClient, ttl_s: float, timeout_s: float = 3.0
  ) -> CreditsSnapshot | None:
      global _cached_snapshot, _cached_fetched_at, _cached_is_failure
      hit, value = _cache_lookup(time.monotonic(), ttl_s)
      if hit:
          return value
      async with _cache_lock:
          hit, value = _cache_lookup(time.monotonic(), ttl_s)
          if hit:
              return value
          now = time.monotonic()
          result = await fetch_openrouter_credits(api_key, client=client, timeout_s=timeout_s)
          _cached_fetched_at = now
          if result is None:
              _cached_is_failure = True
              _cached_snapshot = None
              return None
          _cached_is_failure = False
          _cached_snapshot = result
          return result
  ```

  `_cache_lookup` is a new private module-level helper (pure read of
  the three globals, no mutation) — not exported, no new public name.

## Behavior

### Nominal

- A single caller with a fresh cache: `_cache_lookup` hits on the first
  (lock-free) check, returns immediately — identical latency and result
  to today.
- A single caller with an expired/absent cache: falls through to the
  lock, acquires it uncontended, re-checks (still a miss), fetches,
  stores, returns — same outcome as today, one extra uncontended
  `asyncio.Lock` acquisition.

### Edge cases

- Two callers both observe an expired cache concurrently: the first to
  reach `async with _cache_lock` acquires it and starts the fetch; the
  second blocks on the lock (yielding to the event loop rather than
  starting its own fetch). Once the first finishes and releases the
  lock, the second re-checks the cache under `_cache_lookup`, finds it
  now fresh, and returns that value — `fetch_openrouter_credits` is
  called exactly once for the pair.
- Three or more callers stampede simultaneously: same mechanism,
  generalizes — exactly one fetch, N-1 callers wait on the lock then
  read the fresh cache.
- The in-flight fetch fails (`fetch_openrouter_credits` returns `None`):
  the failure is cached exactly as today (`_cached_is_failure = True`);
  a second caller waiting on the lock re-checks under
  `_cache_lookup`, sees the fresh failure-cache entry, and returns
  `None` without issuing its own request either.

### Failure scenarios

- `fetch_openrouter_credits` never raises (existing contract, NG3) so
  no new exception path is introduced; `async with _cache_lock` still
  guarantees lock release via its context-manager protocol even if some
  future change to the awaited call were to raise, so no deadlock is
  introduced.

## Architecture Impact

- Adds/Removes dependency: none — `asyncio` is stdlib, already used
  elsewhere in the codebase (`src/repoach/llm_proxy/providers/rate_limit.py:51`);
  no new cross-module import, no new src file.
- New / changed coupling, cycles, or shared state: the three existing
  module-level globals gain a fourth sibling, `_cache_lock`, guarding
  the same shared state they already implicitly shared; no new shared
  state crosses a module boundary. `depends_on: [SP-CREDITS-CHECK]` is
  the sole authorizing edge for editing `credits.py`, unchanged from
  today's ownership graph.

## Diagram

N/A (in-place fix confined to one function plus one new private
helper in the same file).

## Acceptance Criteria

- [ ] AC1: unit —
  `tests/unit/test_credits.py::test_concurrent_callers_coalesce_onto_one_fetch`.
  Build an `httpx.MockTransport` handler that appends to a shared
  `call_count: list[int]` on every invocation, `await
  asyncio.sleep(0.01)`, then returns the nominal 200 payload. With a
  cold cache (`reset_credits_cache()`) and a large `ttl_s`, run
  `asyncio.gather(get_cached_credits(...), get_cached_credits(...))`
  with two callers sharing the same `httpx.AsyncClient` built on that
  transport. Assert `len(call_count) == 1` (proves coalescing) and both
  results are not `None`. FAILS on pre-change code — with no lock, both
  coroutines pass the cache-hit check before either awaits the
  transport handler's `asyncio.sleep`, so `len(call_count) == 2`;
  PASSES after the fix.
- [ ] AC2: unit — same file,
  `test_concurrent_callers_share_the_same_snapshot_identity`. Same
  concurrent-gather setup as AC1; assert `result1 is result2` (the
  second caller returns the exact object the first caller's fetch
  produced, read back from the cache under the lock, not a second
  independently-constructed `CreditsSnapshot`). FAILS on pre-change
  code for the same reason as AC1 (two independent fetches produce two
  distinct `CreditsSnapshot` instances, `result1 is result2` is
  `False`); PASSES after the fix.
- [ ] AC3: existing tests in `tests/unit/test_credits.py` —
  `test_fetch_nominal_returns_snapshot`, `test_fetch_errors_return_none`,
  `test_fetch_timeout_returns_none`, `test_cache_ttl_expiry_and_reset`,
  `test_remaining_can_be_negative`, `test_snapshot_is_frozen` — all
  still pass unmodified, proving the single-caller path (NG1) is
  byte-for-byte unaffected.
- [ ] AC4: `ruff check` + `ruff format --check` green; zero inline
  comments (SP-NO-INLINE-COMMENTS-GATE) and no `# noqa` anywhere in the
  diff; full `pytest tests/unit` green; `repoach arch graph --check`
  exits 0 (no new ownership conflict — `owns.code: N/A`, edit made
  under the `depends_on: [SP-CREDITS-CHECK]` edge).

## Open Questions

None.
