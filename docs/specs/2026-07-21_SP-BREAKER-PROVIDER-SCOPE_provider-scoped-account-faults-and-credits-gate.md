---
id: SP-BREAKER-PROVIDER-SCOPE
title: Provider-scoped account faults and a proactive credits gate
version: 0.1
status: approved
author: jfaye (OpenRouter 402 window, 2026-07-10 → 2026-07-21)
created: 2026-07-21
updated: 2026-07-21

owns:
  code: []
  resources: []

depends_on: []
provides_to: []

constraints: {}
---

# Provider-scoped account faults and a proactive credits gate

## Intent

The proxy KNOWS a provider account is dead and still pays to rediscover
it, one ref at a time, after every restart. Since 2026-07-10 the
OpenRouter balance sits below zero: every `open_router/*` hop answers
402. Today that knowledge lives in two places that never meet:

- the breaker learns the 402 — but per REF and in memory. The sonnet
  chain carries four `open_router/*` hops, so a cold proxy pays up to
  four full 402 round-trips per tier to re-learn one single fact (the
  ACCOUNT has no credits), and re-pays the whole class after every
  restart (restarts also happen on every proxy-touching merge);
- the credits snapshot (SP-CREDITS-CHECK) polls the account balance
  and displays LOW on `/health` and `chain-status` — but dispatch
  never consults it.

Close both gaps: an account-class failure on one ref benches every ref
of that provider at once, and a credits balance below the floor keeps
`open_router/*` refs out of dispatch before the first attempt, across
restarts.

## Context

- `src/repoach/llm_proxy/routing/breaker.py` — `QUARANTINE_REASONS`
  groups `auth_failed` / `provider_401` / `provider_402` /
  `provider_403` / `provider_404`; `BreakerState.trip` benches ONE
  `ModelRef`; `ttl_for_reason` already hands the quarantine TTL to the
  whole class. Within that class, 401/402/403 (and `auth_failed`) are
  properties of the provider ACCOUNT — if one `open_router/*` ref
  earns a 402, every other `open_router/*` ref will too. `provider_404`
  is a property of the MODEL ID and must stay per-ref.
- `src/repoach/llm_proxy/api/services.py` — the failover loop
  classifies failures (`_classify_failover_reason`) and trips the
  breaker (`_trip_breaker`); chain resolution already accepts a
  `skip_models` frozenset of provider-prefixed refs
  (SP-PROXY-SEMANTIC-FAILOVER) — the natural seam for a proactive
  skip.
- `src/repoach/health/credits.py` (SP-CREDITS-CHECK) — cached
  OpenRouter balance snapshot (`get_cached_credits`,
  `credits_floor_usd`, `credits_health_cache_ttl_s`), already consumed
  by `/health` in `src/repoach/llm_proxy/api/routes.py`. Credits are
  an OpenRouter-only concept today; no other provider exposes one.

## Goals

- G1 (account-fault propagation): when `_trip_breaker` fires with an
  account-class reason (`auth_failed`, `provider_401`, `provider_402`,
  `provider_403`), every ref of the SAME provider currently present in
  the resolved chains is benched in the same call, with the same
  quarantine TTL and a distinguishable reason (e.g.
  `provider_402_propagated`), and exactly one structured log event
  reports the propagation (provider, ref count, TTL).
- G2 (404 stays per-ref): `provider_404` and every non-account reason
  keep today's single-ref behavior.
- G3 (credits gate): when the cached credits snapshot reports
  `remaining < credits_floor_usd`, chain resolution excludes
  `open_router/*` refs from dispatch for that request — before any
  attempt — and a structured log event states the exclusion once per
  snapshot refresh, not once per request.
- G4 (fail-open): when the credits snapshot is unavailable (probe
  error, cache empty and fetch failing), the gate does NOT exclude
  anything — availability of the sonde must never brick a provider.
- G5 (recovery): a fresh snapshot at or above the floor lifts the gate
  with no restart; a benched provider recovers ref-by-ref exactly as
  today when TTLs lapse.

## Non-Goals

- NG1: no persistence of breaker state across restarts (a separate
  concern; the credits gate already covers the OpenRouter-dead case
  across restarts, which is the live pain).
- NG2: no credits polling for providers that expose no balance API —
  the gate is OpenRouter-only until another provider grows one.
- NG3: no change to reason classification (`_classify_failover_reason`)
  or to TTL arithmetic (`ttl_for_reason`, `escalated_ttl`).
- NG4: no chain re-ordering or head re-selection — refs are skipped,
  the chain shape is untouched.

## Assumptions

- A1: `ModelRef` carries its provider prefix (the `provider/model`
  string form used by `skip_models`) or exposes it trivially.
- A2: the credits snapshot layer (SP-CREDITS-CHECK) is the single
  source for balance reads; this spec adds a consumer, not a second
  poller.
- A3: dispatch code paths that resolve chains are async and may await
  the cached snapshot (routes.py already does).

## Interface

- `BreakerState` gains a provider-scoped bench operation (name at the
  implementer's discretion, e.g. `trip_provider(provider, refs, ...)`)
  used only by the account-fault path in `_trip_breaker`; `trip`,
  `is_down`, `down_refs`, `snapshot`, `clear` keep their signatures.
- Chain resolution accepts the credits-gate exclusions through the
  EXISTING `skip_models` seam (no new public parameter if the gate can
  feed it; a private helper computing the exclusion set is fine).
- `/health` breaker listing shows propagated entries like any other
  benched ref (reason string makes the propagation visible).

## Behavior

### Nominal

One 402 on `open_router/qwen/qwen3.7-max` → all `open_router/*` refs
in the active chains bench for the quarantine TTL in one call; the
next request skips them without a round-trip. Independently: credits
snapshot says `remaining=-0.21 < floor=2.0` → `open_router/*` never
even enters dispatch, cold start included.

### Edge cases

- Mixed chain where the provider's refs are already benched: the
  propagation call is idempotent (re-trip extends per existing trip
  semantics, no duplicate log spam).
- `provider_404` on an `open_router` ref benches only that ref (G2).
- Credits exactly at the floor: NOT below → gate open (strict `<`).
- Snapshot stale beyond its TTL and refresh failing: G4 fail-open,
  plus the existing LOW display keeps warning the operator.
- claude_code / nvidia_nim refs are never touched by the credits gate.

### Failure scenarios

- Credits fetch raising inside dispatch must be swallowed into the
  fail-open path (logged once), never surfacing to the caller.
- Propagation must not mask the ORIGINAL failure handling: the failed
  request still walks the remaining (non-benched) chain exactly as
  today.

## Architecture Impact

- Adds/Removes dependency: `llm_proxy/api/services.py` (or the chain
  resolution module) gains an import of `repoach.health.credits`
  (already imported by `llm_proxy/api/routes.py` — no new package
  coupling, no cycle: `health` imports nothing from `llm_proxy`).
- No new shared state: the propagation writes through the existing
  `BreakerState`; the gate reads the existing cached snapshot.

## Diagram

N/A (two seams widen inside the existing failover loop)

## Acceptance Criteria

- [ ] AC1: unit — new file `tests/unit/test_breaker_provider_scope.py`
  with real `BreakerState` instances (no monkeypatching of repoach
  code): `test_account_fault_benches_all_provider_refs` (402 on one
  open_router ref benches the provider's other chain refs, quarantine
  TTL, propagated reason), `test_404_stays_single_ref`,
  `test_propagation_idempotent_on_rebench`,
  `test_other_provider_refs_untouched`.
- [ ] AC2: unit — new file `tests/unit/test_credits_gate.py` driving
  the exclusion helper with a fake httpx transport at the boundary
  (the SP-CREDITS-CHECK test style):
  `test_below_floor_excludes_open_router_refs`,
  `test_at_floor_keeps_open_router` (strict `<`),
  `test_snapshot_unavailable_fails_open`,
  `test_recovered_balance_lifts_gate_without_restart`.
- [ ] AC3 (INTEGRATION): new file
  `tests/integration/test_provider_scope_and_credits_gate.py::test_one_402_skips_sibling_refs_end_to_end`
  — drive the failover service against fake provider transports (the
  `test_proxy_dead_hop_quarantine.py` style): a chain with two
  `open_router/*` refs and one healthy fallback; the first request
  hits one 402; assert the SECOND `open_router` ref was never called
  (transport call log), the request completes on the fallback, and
  `/health`-shape breaker snapshot lists both refs with the
  propagated reason.
- [ ] AC4: `ruff` + `ruff format --check` green; zero inline comments;
  no `# noqa`; `pytest tests/unit` green including the existing
  breaker suite (`tests/unit/test_health_breaker.py`) unchanged.

## Open Questions

None.
