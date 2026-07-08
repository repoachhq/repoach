# SP-CHAIN-DEAD-HOP-QUARANTINE — Quarantine dead chain hops out of the runtime failover order

Escalate persistently-dead chain hops to a long quarantine TTL so dispatch stops paying their round-trip every 120 s: permanent-config failure reasons (401/402/403/404/auth) quarantine on first trip, any reason quarantines after N consecutive trips with no intervening recovery, and the breaker state becomes observable on /health. Pure policy first, settings knobs second, wiring + observability last. Hand-authored from the spec's suggested decomposition after two Planner sessions exhausted their attempts on plan-form rules.

## Step 1 — Quarantine policy primitives in the health breaker

- **Files**: `src/ferova/llm_proxy/routing/breaker.py`, `tests/unit/test_health_breaker.py`
- **Action**: In src/ferova/llm_proxy/routing/breaker.py add QUARANTINE_REASONS: frozenset[str] = {"auth_failed", "provider_401", "provider_402", "provider_403", "provider_404"}. Extend ttl_for_reason with a quarantine_ttl_s keyword (give it a backward-compatible default so existing call sites and tests stay green) with precedence terminal > quarantine > default: members of QUARANTINE_REASONS get quarantine_ttl_s, provider_410 keeps the terminal TTL, everything else (timeout, empty_completion, rate_limited, unknown) keeps default_ttl_s. Add a pure escalated_ttl(consecutive_failures, *, base_ttl_s, quarantine_ttl_s, threshold) -> float returning max(base_ttl_s, quarantine_ttl_s) when consecutive_failures >= threshold, else base_ttl_s. Give BreakerState a per-ref consecutive-failure counter: trip(...) increments and returns the ref's count (and never shortens an existing trip); recover(ref) clears the trip AND the counter; add BreakerState.snapshot(now) returning a read-only list of entries (ref, reason, ttl_remaining_s, consecutive_failures) for each currently-down ref. Keep BreakerState clock-free (now passed in) and I/O-free. Add the unit tests tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class (quarantine TTL for every QUARANTINE_REASONS member, terminal for provider_410, default for timeout/empty_completion/rate_limited/unknown), tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine (three trips with reason empty_completion → escalated_ttl returns the quarantine TTL at the third; the counter survives TTL lapse between trips), tests/unit/test_health_breaker.py::test_recover_resets_counter (trip, trip, recover, trip → count restarts at 1), and tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs (two tripped refs with distinct reasons → snapshot lists both with reason, remaining TTL and count; a recovered ref disappears).
- **Commit**: `feat(proxy): quarantine policy primitives in health breaker`
- **Done when**: pytest tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine tests/unit/test_health_breaker.py::test_recover_resets_counter tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class`, `tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine`, `tests/unit/test_health_breaker.py::test_recover_resets_counter`, `tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs`

## Step 2 — Quarantine TTL and threshold settings

- **Files**: `src/ferova/llm_proxy/config/settings.py`, `tests/unit/test_health_breaker.py`
- **Action**: In src/ferova/llm_proxy/config/settings.py add breaker_ttl_quarantine_s: float = 21_600.0 with validation alias BREAKER_TTL_QUARANTINE_S (same _aliases pattern as the existing breaker_ttl_s field) and breaker_quarantine_threshold: int = 3 with ge=1 and validation alias BREAKER_QUARANTINE_THRESHOLD. Add the unit test tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases asserting the defaults (21600.0 and 3), that the FEROVA_-prefixed and bare alias forms both override them, and that a threshold of 0 is rejected.
- **Commit**: `feat(proxy): quarantine TTL and threshold settings`
- **Done when**: pytest tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases`

## Step 3 — Wire quarantine escalation into the dispatch breaker path

- **Files**: `src/ferova/llm_proxy/api/services.py`, `tests/unit/test_health_breaker.py`
- **Action**: In src/ferova/llm_proxy/api/services.py make _trip_breaker compose the new policy: compute the reason TTL via ttl_for_reason(..., quarantine_ttl_s=settings.breaker_ttl_quarantine_s), obtain the ref's consecutive-failure count from trip, and apply escalated_ttl(count, base_ttl_s=<reason TTL>, quarantine_ttl_s=settings.breaker_ttl_quarantine_s, threshold=settings.breaker_quarantine_threshold) so the observable contract holds: a QUARANTINE_REASONS failure is down for the quarantine TTL on the first trip, any reason is down for the quarantine TTL from the Nth consecutive trip, and the counter is not double-incremented by escalation (composition mechanics free — e.g. re-trip at the escalated TTL without incrementing, or pass the escalated TTL into a single trip). Emit a structured log event breaker_quarantined with ref, reason, count and ttl_s exactly when the applied TTL is the quarantine one. Add the unit test tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation (fake settings, one ref failing with empty_completion three times → third application is the quarantine TTL and breaker_quarantined fires once; a provider_402 failure gets the quarantine TTL on the first application).
- **Commit**: `feat(proxy): quarantine wiring in dispatch breaker path`
- **Done when**: pytest tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation`

## Step 4 — /health breaker view + dead-hop integration test

- **Files**: `src/ferova/llm_proxy/api/routes.py`, `tests/unit/test_health_breaker.py`, `tests/integration/test_proxy_dead_hop_quarantine.py`
- **Action**: In src/ferova/llm_proxy/api/routes.py extend the /health response with a "breaker" array built from BreakerState.snapshot(now) (ref, reason, ttl_remaining_s, consecutive_failures). Add the unit test tests/unit/test_health_breaker.py::test_health_reports_breaker_entries (route handler with a seeded BreakerState → response JSON carries the breaker array). Create tests/integration/test_proxy_dead_hop_quarantine.py with test_dead_hop_quarantined_and_reported_on_health: build the FastAPI app with a stubbed provider transport in which one chain hop always fails in-stream (empty completion) and the next hop succeeds; drive three requests through the dispatch path with a TestClient, then assert the dead hop is down with a remaining TTL greater than the transient breaker_ttl_s (quarantined, not flapping) and that GET /health lists it in the breaker array with consecutive_failures >= 3 while the healthy hop keeps serving. The test must be hermetic: no network, no reliance on a .env file (provide the auth token and settings via monkeypatch/fixtures — CI runs without .env).
- **Commit**: `feat(proxy): /health breaker view + dead-hop integration test`
- **Done when**: pytest tests/unit/test_health_breaker.py::test_health_reports_breaker_entries tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_health_reports_breaker_entries`

## Step 5 — Counter must survive TTL-lapse pruning in down_refs

- **Files**: `src/ferova/llm_proxy/routing/breaker.py`, `tests/unit/test_health_breaker.py`
- **Action**: In src/ferova/llm_proxy/routing/breaker.py, down_refs currently pops the ref's entry from _consecutive_failures when a lapsed trip is pruned. That violates spec G2: the consecutive-failure count must SURVIVE TTL lapse and reset only on recover (a successful completion) or clear. Remove the _consecutive_failures.pop from the down_refs pruning loop (keep pruning _down_until and _down_reason exactly as is) and state the G2 rationale in the down_refs docstring. Add the unit test tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune: trip a ref with a tiny ttl, advance now past the ttl, call down_refs (which prunes the lapsed trip), trip again and assert the returned count is 2, not 1; then recover and trip again asserting the count restarts at 1. This is the fix for the failure the integration test exposes end-to-end.
- **Commit**: `fix(proxy): consecutive-failure count survives TTL-lapse pruning`
- **Done when**: pytest tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune`

## Integration tests

- `tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-CHAIN-DEAD-HOP-QUARANTINE",
  "title": "Quarantine dead chain hops out of the runtime failover order",
  "summary": "Escalate persistently-dead chain hops to a long quarantine TTL so dispatch stops paying their round-trip every 120 s: permanent-config failure reasons (401/402/403/404/auth) quarantine on first trip, any reason quarantines after N consecutive trips with no intervening recovery, and the breaker state becomes observable on /health. Pure policy first, settings knobs second, wiring + observability last.",
  "steps": [
    {
      "index": 1,
      "title": "Quarantine policy primitives in the health breaker",
      "files": [
        "src/ferova/llm_proxy/routing/breaker.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In src/ferova/llm_proxy/routing/breaker.py add QUARANTINE_REASONS: frozenset[str] = {\"auth_failed\", \"provider_401\", \"provider_402\", \"provider_403\", \"provider_404\"}. Extend ttl_for_reason with a quarantine_ttl_s keyword (give it a backward-compatible default so existing call sites and tests stay green) with precedence terminal > quarantine > default: members of QUARANTINE_REASONS get quarantine_ttl_s, provider_410 keeps the terminal TTL, everything else (timeout, empty_completion, rate_limited, unknown) keeps default_ttl_s. Add a pure escalated_ttl(consecutive_failures, *, base_ttl_s, quarantine_ttl_s, threshold) -> float returning max(base_ttl_s, quarantine_ttl_s) when consecutive_failures >= threshold, else base_ttl_s. Give BreakerState a per-ref consecutive-failure counter: trip(...) increments and returns the ref's count (and never shortens an existing trip); recover(ref) clears the trip AND the counter; add BreakerState.snapshot(now) returning a read-only list of entries (ref, reason, ttl_remaining_s, consecutive_failures) for each currently-down ref. Keep BreakerState clock-free (now passed in) and I/O-free. Add the unit tests tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class (quarantine TTL for every QUARANTINE_REASONS member, terminal for provider_410, default for timeout/empty_completion/rate_limited/unknown), tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine (three trips with reason empty_completion -> escalated_ttl returns the quarantine TTL at the third; the counter survives TTL lapse between trips), tests/unit/test_health_breaker.py::test_recover_resets_counter (trip, trip, recover, trip -> count restarts at 1), and tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs (two tripped refs with distinct reasons -> snapshot lists both with reason, remaining TTL and count; a recovered ref disappears).",
      "commit_message": "feat(proxy): quarantine policy primitives in health breaker",
      "done_when": "pytest tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine tests/unit/test_health_breaker.py::test_recover_resets_counter tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_ttl_for_reason_quarantine_class",
        "tests/unit/test_health_breaker.py::test_consecutive_failures_escalate_to_quarantine",
        "tests/unit/test_health_breaker.py::test_recover_resets_counter",
        "tests/unit/test_health_breaker.py::test_snapshot_lists_down_refs"
      ]
    },
    {
      "index": 2,
      "title": "Quarantine TTL and threshold settings",
      "files": [
        "src/ferova/llm_proxy/config/settings.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In src/ferova/llm_proxy/config/settings.py add breaker_ttl_quarantine_s: float = 21_600.0 with validation alias BREAKER_TTL_QUARANTINE_S (same _aliases pattern as the existing breaker_ttl_s field) and breaker_quarantine_threshold: int = 3 with ge=1 and validation alias BREAKER_QUARANTINE_THRESHOLD. Add the unit test tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases asserting the defaults (21600.0 and 3), that the FEROVA_-prefixed and bare alias forms both override them, and that a threshold of 0 is rejected.",
      "commit_message": "feat(proxy): quarantine TTL and threshold settings",
      "done_when": "pytest tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_quarantine_settings_defaults_and_aliases"
      ]
    },
    {
      "index": 3,
      "title": "Wire quarantine escalation into the dispatch breaker path",
      "files": [
        "src/ferova/llm_proxy/api/services.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In src/ferova/llm_proxy/api/services.py make _trip_breaker compose the new policy: compute the reason TTL via ttl_for_reason(..., quarantine_ttl_s=settings.breaker_ttl_quarantine_s), obtain the ref's consecutive-failure count from trip, and apply escalated_ttl(count, base_ttl_s=<reason TTL>, quarantine_ttl_s=settings.breaker_ttl_quarantine_s, threshold=settings.breaker_quarantine_threshold) so the observable contract holds: a QUARANTINE_REASONS failure is down for the quarantine TTL on the first trip, any reason is down for the quarantine TTL from the Nth consecutive trip, and the counter is not double-incremented by escalation (composition mechanics free — e.g. re-trip at the escalated TTL without incrementing, or pass the escalated TTL into a single trip). Emit a structured log event breaker_quarantined with ref, reason, count and ttl_s exactly when the applied TTL is the quarantine one. Add the unit test tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation (fake settings, one ref failing with empty_completion three times -> third application is the quarantine TTL and breaker_quarantined fires once; a provider_402 failure gets the quarantine TTL on the first application).",
      "commit_message": "feat(proxy): quarantine wiring in dispatch breaker path",
      "done_when": "pytest tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_trip_breaker_composes_escalation"
      ]
    },
    {
      "index": 4,
      "title": "/health breaker view + dead-hop integration test",
      "files": [
        "src/ferova/llm_proxy/api/routes.py",
        "tests/unit/test_health_breaker.py",
        "tests/integration/test_proxy_dead_hop_quarantine.py"
      ],
      "action": "In src/ferova/llm_proxy/api/routes.py extend the /health response with a \"breaker\" array built from BreakerState.snapshot(now) (ref, reason, ttl_remaining_s, consecutive_failures). Add the unit test tests/unit/test_health_breaker.py::test_health_reports_breaker_entries (route handler with a seeded BreakerState -> response JSON carries the breaker array). Create tests/integration/test_proxy_dead_hop_quarantine.py with test_dead_hop_quarantined_and_reported_on_health: build the FastAPI app with a stubbed provider transport in which one chain hop always fails in-stream (empty completion) and the next hop succeeds; drive three requests through the dispatch path with a TestClient, then assert the dead hop is down with a remaining TTL greater than the transient breaker_ttl_s (quarantined, not flapping) and that GET /health lists it in the breaker array with consecutive_failures >= 3 while the healthy hop keeps serving. The test must be hermetic: no network, no reliance on a .env file (provide the auth token and settings via monkeypatch/fixtures — CI runs without .env).",
      "commit_message": "feat(proxy): /health breaker view + dead-hop integration test",
      "done_when": "pytest tests/unit/test_health_breaker.py::test_health_reports_breaker_entries tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_health_reports_breaker_entries"
      ]
    }
    ,{
      "index": 5,
      "title": "Counter must survive TTL-lapse pruning in down_refs",
      "files": [
        "src/ferova/llm_proxy/routing/breaker.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In src/ferova/llm_proxy/routing/breaker.py, down_refs currently pops the ref's entry from _consecutive_failures when a lapsed trip is pruned. That violates spec G2: the consecutive-failure count must SURVIVE TTL lapse and reset only on recover (a successful completion) or clear. Remove the _consecutive_failures.pop from the down_refs pruning loop (keep pruning _down_until and _down_reason exactly as is) and state the G2 rationale in the down_refs docstring. Add the unit test tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune: trip a ref with a tiny ttl, advance now past the ttl, call down_refs (which prunes the lapsed trip), trip again and assert the returned count is 2, not 1; then recover and trip again asserting the count restarts at 1. This is the fix for the failure the integration test exposes end-to-end.",
      "commit_message": "fix(proxy): consecutive-failure count survives TTL-lapse pruning",
      "done_when": "pytest tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_counter_survives_ttl_lapse_prune"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_proxy_dead_hop_quarantine.py::test_dead_hop_quarantined_and_reported_on_health"
  ]
}
```
