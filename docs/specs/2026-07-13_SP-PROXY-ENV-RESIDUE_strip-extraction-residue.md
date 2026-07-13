---
id: SP-PROXY-ENV-RESIDUE
title: Strip extraction residue — foreign env path, PTB line, dead singleton, stale docstring, empty trip reason, breaker recovery on budget retry
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

# Strip extraction residue — foreign env path, PTB line, dead singleton, stale docstring, empty trip reason, breaker recovery on budget retry

## Intent

A cluster of low-severity residue from the proxy's extraction: a foreign
out-of-repo env file is read first (can inject keys/HOST/PORT), a
python-telegram-bot env var is set though PTB is not a dependency, a dead
singleton guard lingers, a docstring lies about being unwired, a seeded breaker
trip omits its reason, and a budget-retry success neither recovers the breaker
nor logs recovery. Remove or correct each.

## Context

Audit 2026-07-13 finding M25 + proxy low-severity residue:
- `src/ferova/llm_proxy/config/settings.py:35` — `_env_files()` reads
  `Path.home()/".config"/"free-claude-code"/".env"` FIRST (foreign,
  out-of-repo, from the pre-extraction project) before `.env` and `chains.env`;
  a stray file there can inject provider keys, `HOST`/`PORT`, or a token.
- `src/ferova/llm_proxy/api/app.py:20` — `os.environ["PTB_TIMEDELTA"] = "1"`
  opts into a python-telegram-bot behavior; PTB is not a dependency of this
  repo.
- `src/ferova/llm_proxy/providers/rate_limit.py:40-41` — the
  `if hasattr(self, "_initialized"): return` singleton guard on `__init__` is
  dead (the class is instantiated fresh per scope via `get_scoped_instance`,
  `rate_limit.py:86-114`); the vestigial process-wide `_instance` /
  `get_instance` singleton path (`rate_limit.py:31,64-83`) is unused residue.
- `src/ferova/llm_proxy/providers/effort_map.py:12-15` — the docstring claims
  "unwired: nothing seeds it at startup and nothing reads it in production yet",
  but `AppRuntime._seed_effort_map` (`api/runtime.py:63-81`) seeds it at startup
  and `openai_generic.py:85` reads it on the hot path.
- `src/ferova/llm_proxy/routing/probe_seed.py:100` — `breaker.trip(head,
  now=now, ttl_s=ttl_s)` omits `reason=`, so a probe-seeded trip surfaced on
  `/health` carries an empty reason (every other `trip` call passes `reason`).
- `src/ferova/llm_proxy/api/services.py:309-320` — a successful budget-retry
  (`_retry_with_more_budget` returns content) yields the buffered chunks and
  returns, but does NOT call `get_breaker().recover(...)` for the candidate nor
  log a recovery, unlike the normal success path (`services.py:290-291`); spec
  G2 semantics say the consecutive-failure counter resets on success.

## Goals

- G1: `_env_files()` no longer includes the foreign
  `~/.config/free-claude-code/.env` path; the load order is `.env` (optional
  `FCC_ENV_FILE`) then `chains.env` (authoritative for chains, unchanged).
- G2: the `PTB_TIMEDELTA` line at `app.py:20` (and its docstring) is removed.
- G3: the dead singleton guard and the vestigial `_instance`/`get_instance`
  path in `rate_limit.py` are removed (keep the live `get_scoped_instance`
  path); no behavior change for scoped limiters.
- G4: the `effort_map.py` module docstring is corrected to state it IS wired —
  seeded at `AppRuntime.startup` and read by the generic transport.
- G5: the probe-seed trip at `probe_seed.py:100` passes `reason=` (the
  `reason_from_detail(row.detail)` already computed at `probe_seed.py:94`), so
  `/health` shows a real reason for seeded trips.
- G6: a successful budget-retry recovers the breaker for the candidate
  (`get_breaker().recover(...)`) and logs a recovery event, matching the normal
  success path and the G2-counter-resets-on-success rule.

## Non-Goals

- NG1: no change to `chains.env` precedence (SP-CHAINS-SINGLE-SOURCE) — it stays
  authoritative and last.
- NG2: no change to the budget-retry enlargement math (`_retry_with_more_budget`
  body) beyond adding the recovery+log on success.
- NG3: no removal of `get_scoped_instance` or the live rate-limiter behavior.
- NG4: no new module — every change is in-place in an already-owned file.

## Assumptions

- A1: nothing in the repo depends on `~/.config/free-claude-code/.env` being
  loaded (it is foreign extraction residue).
- A2: no code path calls `GlobalRateLimiter.get_instance` (grep-verify during
  implementation); only `get_scoped_instance` is live.
- A3: `PTB_TIMEDELTA` affects only python-telegram-bot, which is not installed.

## Interface

N/A (in-place fixes, no signature change) except the removal of the unused
`GlobalRateLimiter.get_instance` / `_instance` members (dead-code removal, no
live caller).

## Behavior

### Nominal

- Settings load from `.env` then `chains.env` only; an explicit `os.environ`
  value still wins (unchanged).
- Scoped rate limiters behave exactly as before.
- A budget-retry that succeeds serves content AND resets the candidate's breaker
  failure counter (recover) and logs `proxy_chain_failover_recovered`-style
  recovery.
- Probe-seeded breaker trips carry their reason on `/health`.

### Edge cases

- No `.env` present -> only `chains.env` (and any `FCC_ENV_FILE`) loaded; no
  foreign path attempted.
- Budget-retry returns no content -> no recovery (unchanged; only success
  recovers).

### Failure scenarios

- Removing the foreign env path fails CLOSED against silent config injection: a
  stray out-of-repo file can no longer seed keys/HOST/PORT into the proxy.

## Architecture Impact

- Adds dependency: none — all in-place modifications of files owned by existing
  specs (`settings.py`, `app.py`, `rate_limit.py`, `effort_map.py`,
  `probe_seed.py`, `services.py`). No new cross-owner import; dead-code removal
  only reduces surface.
- New / changed coupling, cycles, or shared state: coupling DECREASES (dead
  singleton removed); no cycle.

## Diagram

N/A (bundle of in-place fixes).

## Acceptance Criteria

- [ ] AC1: unit — `_env_files()` no longer contains any
  `free-claude-code` path (assert on the resolved tuple); `chains.env` remains
  last. `GlobalRateLimiter` has no `get_instance`/`_instance` member and scoped
  limiters still construct. `effort_map` module docstring asserts it is wired
  (assert the "unwired" wording is gone — a doc/text assertion). `probe_seed`
  trip passes a non-empty `reason`.
- [ ] AC2 (INTEGRATION): two real-flow checks — (a) construct the llm_proxy
  `Settings` and assert the resolved `env_file` list excludes the foreign path
  (drive the real settings load, no monkeypatching of Ferova code); (b) drive
  `_stream_with_failover` on a candidate that first returns a budget-starved
  empty completion then, on the enlarged budget retry, real content (providers
  backed by `httpx.MockTransport` truthful boundary fakes) and assert the real
  breaker's consecutive-failure counter for that candidate is reset to 0 (the
  budget-retry success recovered it) and a recovery log event was emitted.
- [ ] AC3: promised test file + selectors —
  `tests/unit/test_proxy_env_residue.py::test_env_files_excludes_foreign_path`,
  `::test_rate_limiter_singleton_removed`,
  `::test_probe_seed_trip_carries_reason`,
  `tests/unit/test_proxy_budget_retry_recovers_breaker.py::test_budget_retry_success_resets_breaker_counter`.
- [ ] AC4: `ruff` + `ruff format --check` + `pytest tests/unit` green; zero
  inline comments (SP-NO-INLINE-COMMENTS-GATE); no `# noqa`;
  `ferova arch graph --check` exits 0.

## Open Questions

(none)
