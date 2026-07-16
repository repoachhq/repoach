---
id: SP-CREDITS-CHECK
title: OpenRouter credits floor — probe, surface, degrade
version: 0.1
status: approved
author: jfaye (2026-07-10 incident PR #76; architecture docs/chain_resilience_architecture.md W1.4; adversarial panel 2026-07-11)
created: 2026-07-11
updated: 2026-07-11

owns:
  code: [src/ferova/health/credits.py]
  resources: []

depends_on: []
provides_to: []

constraints:
  credits_floor_usd_default: 2.0
  health_cache_ttl_s_default: 3600
---

# OpenRouter credits floor — probe, surface, degrade

## Intent

Surface provider credit exhaustion BEFORE it silently kills fallback
hops. During the 2026-07-10 incident, OpenRouter credits were
exhausted (`total_usage 20.21 / total_credits 20`) and both OpenRouter
refs in the sonnet chain 402ed — discovered mid-incident by manual
curl. Nothing in the codebase polls the balance — the only executable
trace of credits handling is the 402 failover fixture
(`tests/unit/test_proxy_402_failover.py:30`).

## Context

`monitor-chains` (`src/ferova/cli/main.py:67-128`) runs every 15 min
via `ferova-nim-health.timer`, probes each tier's NIM head with an
injected `httpx.AsyncClient` (`check_tier_heads`,
`src/ferova/review/chain_health.py:194`), persists to
`nim_health_probe`, and raises `typer.Exit(1)` when any tier
`is_degraded` — tolerated by the unit's `SuccessExitStatus=0 1`. The
OpenRouter key is already configured
(`Settings.open_router_api_key`,
`src/ferova/llm_proxy/config/settings.py:214`). `GET /health`
(`src/ferova/llm_proxy/api/routes.py:150-173`) reports the breaker
snapshot. This spec is minimal by design (architecture W1.4): no
history table, no new timer — current-value-vs-threshold is the whole
job. Deviation from the W1.4 text: the fetch runs on EVERY
monitor-chains run, not every Nth — one GET per 15 min is negligible
and keeps the CLI stateless (the architecture doc is aligned in the
same PR).

## Goals

- G1: a reusable helper fetches the OpenRouter credits snapshot and
  never raises.
- G2: every `monitor-chains` run reports the snapshot, and a
  below-floor balance degrades the run (loud log + `Exit(1)`).
- G3: `GET /health` carries a `credits` field, populated by a lazy
  TTL-cached lookup that can never break the endpoint.
- G4: the helper is directly consumable by SP-CHAIN-STATUS-DIGEST and
  SP-REGEN-FRESH-CELLS (their `depends_on` points here).

## Non-Goals

- NG1: no persistence of credit history (no table, no store module).
- NG2: no per-provider generalization — OpenRouter is the only known
  credits endpoint; a descriptor extension waits for a second need.
- NG3: no push notification (wave 2) and no automatic top-up.
- NG4: no new systemd unit or timer.

## Assumptions

- A1: `GET https://openrouter.ai/api/v1/credits` with
  `Authorization: Bearer <key>` returns
  `{"data": {"total_credits": <float>, "total_usage": <float>}}`
  (verified live 2026-07-10).
- A2: an absent/empty `open_router_api_key` means the operator does
  not use OpenRouter — the check must skip silently, not degrade.

## Interface

New module `src/ferova/health/credits.py`:

Inputs:
- `fetch_openrouter_credits(api_key: str, *, client: httpx.AsyncClient, timeout_s: float = 10.0) -> CreditsSnapshot | None`
  — one GET against the credits endpoint. Returns `None` on any
  transport error, non-2xx, or unexpected payload shape (logged as a
  `structlog` warning `openrouter_credits_unavailable`); never raises.
- `get_cached_credits(api_key: str, *, client: httpx.AsyncClient, ttl_s: float, timeout_s: float = 3.0) -> CreditsSnapshot | None`
  — the cached accessor the `/health` route calls; the CALLER passes
  the TTL so `ferova.health.credits` never imports llm_proxy
  settings.
- `CreditsSnapshot` (frozen pydantic model): `total_credits: float`,
  `total_usage: float`, and a computed `remaining: float`
  (`total_credits - total_usage`, may be negative — not clamped, the
  true deficit is informative).
- Consumers import `ferova.health.credits` DIRECTLY —
  `src/ferova/health/__init__.py` (owned by
  SP-HEALTH-STORE-NEUTRALIZE, `depends_on: []`) must NOT be modified:
  re-exporting the new module there would create an undeclared
  edge-honesty edge.

Settings (pydantic, `FEROVA_*` aliases, in the llm_proxy `Settings`):
- `credits_floor_usd: float = 2.0` — below this `remaining`, the
  balance is LOW.
- `credits_health_cache_ttl_s: float = 3600.0` — `/health` cache TTL.

Test seams (designed injection points, part of the contract):
- `monitor_chains` builds its probe client through a module-level
  factory in `src/ferova/cli/main.py` (e.g.
  `_probe_client() -> httpx.AsyncClient`) shared by `check_tier_heads`
  and `fetch_openrouter_credits`; tests override THIS factory with
  `httpx.AsyncClient(transport=httpx.MockTransport(...))` — the
  sanctioned truthful boundary fake for CLI-level tests (today the
  client is constructed inline at `cli/main.py:105`, uninjectable).
- the `/health` lazy fetch obtains its client through a
  `get_credits_client` FastAPI dependency (overridable via
  `app.dependency_overrides`, the pattern of `get_settings` in
  `api/dependencies.py:24-26`), and `health/credits.py` exports
  `reset_credits_cache()` mirroring `reset_breaker`
  (`tests/unit/test_health_breaker.py:24,380`) so tests start from a
  cold cache.

Outputs:
- `monitor-chains` stdout gains one line:
  `credits open_router [remaining=<x> floor=<y>] <ok|LOW|unavailable|skipped>`
  (the bracketed fields render only when a snapshot exists). With
  `--json` (`cli/main.py:120-121` emits a bare JSON array), the
  plain-text line is suppressed and a trailing object is ALWAYS
  appended AS THE FINAL ELEMENT of the emitted JSON array (stdout
  stays one parseable document), for all four statuses:
  `{"kind": "credits", "status": "ok|LOW|unavailable|skipped",
  "total_credits": x|null, "total_usage": y|null,
  "remaining": z|null, "floor": f}` (numeric fields null when no
  snapshot exists).
- `GET /health` response gains
  `"credits": {"open_router": {"total_credits": x, "total_usage": y, "remaining": z}} | null`.

Errors:
- none raised by the helper (G1); the CLI exit path reuses the
  existing degraded `typer.Exit(1)`.

## Behavior

### Nominal

`monitor_chains`, after the tier probes and persistence: if
`open_router_api_key` is set, call `fetch_openrouter_credits` with
the client the command builds via the `_probe_client` factory (the
same client that served `check_tier_heads`); log a structlog event
`openrouter_credits` (`remaining`, `floor`, `status`); print the
stdout line. `remaining >= floor` → status `ok`, no effect on exit
code.

`GET /health`: a module-level `(snapshot, fetched_at_monotonic)`
cache; when older than `credits_health_cache_ttl_s` (or empty),
re-fetch lazily with a shorter per-call timeout (`timeout_s=3.0` at
this call site, so a slow credits endpoint can never stall the
health surface); on `None` result, serve `"credits": null` AND cache
the failure for `min(60.0, credits_health_cache_ttl_s)` seconds — a
failed refresh discards the expired snapshot (never served stale) and
`null` is served until a fetch succeeds. The endpoint's existing
fields are unaffected.

### Edge cases

- No API key configured → stdout `credits open_router skipped`, no
  fetch, no degradation, `/health` serves `null`.
- EXISTING TESTS (in scope, promised): every pre-existing
  `monitor-chains` CLI test invokes the command and would otherwise
  fire a LIVE credits GET (the llm_proxy `Settings` loads the repo
  `.env`, `settings.py:20-42`, which carries a real key) and become
  balance-dependent — an exit-code assertion flips whenever the live
  balance is below floor. The affected tests, ALL edited in-place to
  pin `monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")` (an
  explicit env value beats the `.env` files): in
  `tests/unit/test_chain_health.py` —
  `test_cli_exit_code_reflects_worst_status` and
  `test_cli_exit_zero_when_all_healthy`; in
  `tests/unit/test_chain_health_store.py` —
  `test_cli_no_persist_skips` (the exit-0 case that actually breaks)
  and `test_cli_persists_probes` (pinned for correctness so it asserts
  exit 1 from tier degradation, not from a live LOW balance). Key-state
  pinning is the general rule and applies to EVERY `monitor-chains`
  CLI test in the suite, not only the enumerated ones: CLI tests set
  the key deterministically via `setenv` (sentinel or empty) OR
  override the `_probe_client` factory with a MockTransport; `/health`
  tests control it through the settings dependency override
  (`app.dependency_overrides`), never through ambient env.
- Payload missing keys / non-numeric → helper returns `None`
  (status `unavailable`) — `unavailable` does NOT degrade the run
  (a flaky credits endpoint must not page the operator; only a
  confirmed LOW balance does).
- `remaining < 0` (over-consumed, the 2026-07-10 state) → LOW.

### Failure scenarios

- Credits endpoint down → `unavailable` path everywhere; monitor
  exit code unchanged; `/health` stays 200 with `credits: null`.
- `remaining < floor` → status `LOW`, structlog warning
  `openrouter_credits_low`, and the run exits via the existing
  degraded `Exit(1)` path even when all tier heads are healthy.

## Architecture Impact

- Adds dependency: SP-CHAIN-STATUS-DIGEST -> SP-CREDITS-CHECK and
  SP-REGEN-FRESH-CELLS -> SP-CREDITS-CHECK (both consume
  `fetch_openrouter_credits` / its snapshot semantics).
- Ownership narrowing (same PR): SP-HEALTH-STORE-NEUTRALIZE's
  `owns.code` is narrowed from `[src/ferova/health/]` to its concrete
  modules (`__init__.py`, `model_health.py`, `store.py`) with a
  version bump — it cedes `credits.py` to this spec; subtree
  ownership becomes per-module, keeping the deriver's disjointness
  invariant (`Registry.disjointness_violations`,
  `src/ferova/arch/registry.py:245-258`).
- New / changed coupling, cycles, or shared state: none —
  `ferova.health` stays neutral (SP-HEALTH-STORE-NEUTRALIZE), the new
  module imports only httpx/pydantic/logging.

## Diagram

N/A (trivial slice: one helper, two call sites).

## Acceptance Criteria

- [ ] AC1: `fetch_openrouter_credits` unit tests use
  `httpx.MockTransport` (truthful boundary fake — no monkeypatching
  of ferova code): nominal payload → correct snapshot;
  500 / malformed JSON / missing keys / timeout → `None`, no raise.
- [ ] AC2: CLI integration — `monitor-chains` driven through
  `CliRunner`, overriding the `_probe_client` factory (the Interface's
  designed seam) with a `MockTransport`-backed client answering both
  the NIM probe POSTs and the credits GET: (a) healthy heads +
  `remaining < floor` → exit code 1 and the `LOW` line on stdout;
  (b) `remaining >= floor` → exit 0, `ok` line; (c) key pinned empty
  via `monkeypatch.setenv` → `skipped` line, no credits request
  recorded by the transport; (d) one `--json` run asserts the
  trailing `kind="credits"` object shape.
- [ ] AC3: `/health` integration via FastAPI `TestClient`, with
  `get_credits_client` overridden through `app.dependency_overrides`
  and `reset_credits_cache()` called in setup, with the API key
  pinned non-empty through the settings dependency override (the
  key-state pinning rule — deterministic on keyless environments):
  two calls within the TTL perform exactly one upstream GET (assert
  transport call count); upstream failure → `"credits": null` and
  HTTP 200.
- [ ] AC4: `ruff` clean, no inline comments
  (SP-NO-INLINE-COMMENTS-GATE), full `pytest tests/unit` green —
  including the two in-place-edited tests of
  `tests/unit/test_chain_health.py` (see Edge cases), which must stay
  offline and balance-independent.
- [ ] AC5: `health/credits.py` ≤ 100 LOC; ≤ 2 new files (the module +
  one test module, the test module excluded from the LOC cap);
  `monitor_chains`, `/health`, settings and
  `tests/unit/test_chain_health.py` edits stay in-place.
- [ ] AC6: `ferova arch graph --check` exits 0 with this spec and the
  SP-HEALTH-STORE-NEUTRALIZE narrowing both in the tree (the
  ownership disjointness invariant holds).

## Open Questions

(none)
