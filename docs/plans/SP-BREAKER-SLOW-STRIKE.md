# SP-BREAKER-SLOW-STRIKE — Implement k-of-n slow-completion breaker policy (shadow-first)

Add a slow-completion breaker policy that treats chronic slowness as a strike. A pure function is_slow_completion gates on latency and tokens-per-second. PeekResult gains final_output_tokens. BreakerState gains per-ref slow-success history with k-of-n window tracking, a dedicated trip_slow that bypasses consecutive-failure escalation, and record_success. services.py applies the policy on both the primary success path and the budget-retry success path. New settings under breaker_slow_* configure gates, k/n, TTL, and shadow mode. Shadow-first: with breaker_slow_shadow=true (default) the policy logs would_trip without enforcing; flipping to false trips the ref for slow_ttl_s.

## Step 1 — Expose final_output_tokens on PeekResult

- **Files**: `src/repoach/llm_proxy/api/_failover.py`, `tests/unit/test_slow_completion_policy.py`
- **Action**: Add final_output_tokens: int | None = None field to PeekResult dataclass. In peek_for_content, populate the field from the existing local final_output_tokens variable before returning the PeekResult. Create tests/unit/test_slow_completion_policy.py and add test_peek_result_carries_output_tokens (drains a stream carrying a message_delta with output_tokens: 7 and asserts peek.final_output_tokens == 7) and test_peek_result_output_tokens_none_on_absent_delta (stream without a message_delta asserts None).
- **Commit**: `feat(proxy): expose final_output_tokens on PeekResult`
- **Done when**: pytest tests/unit/test_slow_completion_policy.py::test_peek_result_carries_output_tokens tests/unit/test_slow_completion_policy.py::test_peek_result_output_tokens_none_on_absent_delta passes
- **Unit tests**: `tests/unit/test_slow_completion_policy.py::test_peek_result_carries_output_tokens`, `tests/unit/test_slow_completion_policy.py::test_peek_result_output_tokens_none_on_absent_delta`

## Step 2 — Add is_slow_completion pure-policy function

- **Files**: `src/repoach/llm_proxy/routing/slow_policy.py`, `src/repoach/llm_proxy/routing/__init__.py`, `tests/unit/test_slow_completion_policy.py`
- **Action**: Create src/repoach/llm_proxy/routing/slow_policy.py with module docstring stating live-dispatch-vs-offline slowness divergence. Implement is_slow_completion(latency_s, output_tokens, *, gate_s, tps_floor) -> bool returning True iff latency_s > gate_s AND output_tokens is not None AND output_tokens / latency_s < tps_floor. Export is_slow_completion from __init__.py. In test_slow_completion_policy.py add test_is_slow_below_gate_returns_false, test_is_slow_slow_when_above_gate_and_low_tps, test_is_slow_none_tokens_returns_false, test_is_slow_boundaries, test_is_slow_never_raises.
- **Commit**: `feat(breaker): add is_slow_completion pure policy`
- **Done when**: pytest tests/unit/test_slow_completion_policy.py::test_is_slow_below_gate_returns_false tests/unit/test_slow_completion_policy.py::test_is_slow_slow_when_above_gate_and_low_tps tests/unit/test_slow_completion_policy.py::test_is_slow_none_tokens_returns_false tests/unit/test_slow_completion_policy.py::test_is_slow_boundaries tests/unit/test_slow_completion_policy.py::test_is_slow_never_raises passes
- **Unit tests**: `tests/unit/test_slow_completion_policy.py::test_is_slow_below_gate_returns_false`, `tests/unit/test_slow_completion_policy.py::test_is_slow_slow_when_above_gate_and_low_tps`, `tests/unit/test_slow_completion_policy.py::test_is_slow_none_tokens_returns_false`, `tests/unit/test_slow_completion_policy.py::test_is_slow_boundaries`, `tests/unit/test_slow_completion_policy.py::test_is_slow_never_raises`

## Step 3 — Add slow-success k-of-n history to BreakerState

- **Files**: `src/repoach/llm_proxy/routing/breaker.py`, `tests/unit/test_health_breaker.py`
- **Action**: In breaker.py, add _slow_history: dict[ModelRef, list[bool]] to BreakerState.__init__. Implement record_success(ref, slow, *, k, n) -> bool that appends slow, truncates to last n, and returns True if at least k True. Implement trip_slow(ref, *, now, ttl_s, reason='slow_completion') setting _down_until[ref] and _down_reason[ref] without touching _consecutive_failures. Extend recover() to pop ref from _slow_history; ensure down_refs() does not prune slow history; extend clear(). In test_health_breaker.py add test_record_success_k_of_n_window, test_record_success_below_k_returns_false, test_recover_clears_slow_history, test_trip_slow_behavior, test_slow_history_survives_down_refs_prune.
- **Commit**: `feat(breaker): add slow-success k-of-n history and trip_slow`
- **Done when**: pytest tests/unit/test_health_breaker.py -k 'test_record_success or test_recover_clears_slow_history or test_trip_slow_behavior or test_slow_history_survives_down_refs_prune' passes
- **Unit tests**: `tests/unit/test_health_breaker.py::test_record_success_k_of_n_window`, `tests/unit/test_health_breaker.py::test_record_success_below_k_returns_false`, `tests/unit/test_health_breaker.py::test_recover_clears_slow_history`, `tests/unit/test_health_breaker.py::test_trip_slow_behavior`, `tests/unit/test_health_breaker.py::test_slow_history_survives_down_refs_prune`

## Step 4 — Add breaker_slow_* settings to Settings model

- **Files**: `src/repoach/llm_proxy/config/settings.py`, `tests/unit/test_slow_completion_policy.py`
- **Action**: In settings.py, after existing breaker fields add breaker_slow_latency_gate_s: float = 10.0, breaker_slow_tps_floor: float = 1.0, breaker_slow_k: int = 3, breaker_slow_n: int = 5, breaker_slow_ttl_s: float = 300.0, breaker_slow_shadow: bool = True, each declared with validation_alias=_aliases('BREAKER_SLOW_<NAME>') and a matching entry added to _LEGACY_TO_REPOACH_ALIAS, exactly like the existing breaker knobs (BREAKER_TTL_S et al.). In test_slow_completion_policy.py add test_slow_settings_defaults that instantiates Settings and asserts each slow-default value.
- **Commit**: `feat(config): add breaker_slow_* settings`
- **Done when**: pytest tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults passes
- **Unit tests**: `tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults`

## Step 5 — Apply slow-completion policy hook in services.py (primary and budget-retry paths)

- **Files**: `src/repoach/llm_proxy/api/services.py`, `tests/unit/test_slow_breaker_wiring.py`
- **Action**: In services.py, import is_slow_completion from routing.slow_policy and the slow settings from config. At the primary success hook (after current recover call), add logic: compute slow = is_slow_completion(attempt_latency_s, peek.final_output_tokens, gate_s=settings.breaker_slow_latency_gate_s, tps_floor=settings.breaker_slow_tps_floor); call breaker.record_success(...) and branch on shadow vs. enforcement. At the budget-retry success point (after retry_peek success), compute retry latency as time.monotonic() - attempt_started and use retry_peek.final_output_tokens; apply the same slow/recover logic. Create tests/unit/test_slow_breaker_wiring.py with test_slow_policy_hook_shadow_mode, test_slow_policy_hook_enforcing_mode, test_slow_policy_below_k_no_trip, test_slow_policy_fast_success_recovers driving the REAL is_slow_completion and a REAL BreakerState — fake only the provider/stream boundary (controlled-latency fake streams carrying chosen output_tokens) and the clock where needed; never replace repoach functions.
- **Commit**: `feat(proxy): apply slow-completion breaker policy in success paths`
- **Done when**: pytest tests/unit/test_slow_breaker_wiring.py passes
- **Unit tests**: `tests/unit/test_slow_breaker_wiring.py::test_slow_policy_hook_shadow_mode`, `tests/unit/test_slow_breaker_wiring.py::test_slow_policy_hook_enforcing_mode`, `tests/unit/test_slow_breaker_wiring.py::test_slow_policy_below_k_no_trip`, `tests/unit/test_slow_breaker_wiring.py::test_slow_policy_fast_success_recovers`

## Step 6 — Integration test for slow-completion breaker end-to-end

- **Files**: `tests/integration/test_slow_breaker.py`
- **Action**: Create tests/integration/test_slow_breaker.py following the truthful-boundary-fake pattern of test_proxy_dead_hop_quarantine.py. Implement test_slow_breaker_integration_shadow_mode that verifies shadow logging without tripping, and test_slow_breaker_integration_enforcing_mode that verifies a slow_completion trip in /health after k slow completions with shrunk gate.
- **Commit**: `test(proxy): add integration test for slow-completion breaker`
- **Done when**: pytest tests/integration/test_slow_breaker.py passes
- **Unit tests**: `tests/integration/test_slow_breaker.py::test_slow_breaker_integration_shadow_mode`, `tests/integration/test_slow_breaker.py::test_slow_breaker_integration_enforcing_mode`

## Integration tests

- `tests/integration/test_slow_breaker.py::test_slow_breaker_integration_shadow_mode`
- `tests/integration/test_slow_breaker.py::test_slow_breaker_integration_enforcing_mode`

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-BREAKER-SLOW-STRIKE",
  "title": "Implement k-of-n slow-completion breaker policy (shadow-first)",
  "summary": "Add a slow-completion breaker policy that treats chronic slowness as a strike. A pure function is_slow_completion gates on latency and tokens-per-second. PeekResult gains final_output_tokens. BreakerState gains per-ref slow-success history with k-of-n window tracking, a dedicated trip_slow that bypasses consecutive-failure escalation, and record_success. services.py applies the policy on both the primary success path and the budget-retry success path. New settings under breaker_slow_* configure gates, k/n, TTL, and shadow mode. Shadow-first: with breaker_slow_shadow=true (default) the policy logs would_trip without enforcing; flipping to false trips the ref for slow_ttl_s.",
  "steps": [
    {
      "index": 1,
      "title": "Expose final_output_tokens on PeekResult",
      "files": [
        "src/repoach/llm_proxy/api/_failover.py",
        "tests/unit/test_slow_completion_policy.py"
      ],
      "action": "Add final_output_tokens: int | None = None field to PeekResult dataclass. In peek_for_content, populate the field from the existing local final_output_tokens variable before returning the PeekResult. Create tests/unit/test_slow_completion_policy.py and add test_peek_result_carries_output_tokens (drains a stream carrying a message_delta with output_tokens: 7 and asserts peek.final_output_tokens == 7) and test_peek_result_output_tokens_none_on_absent_delta (stream without a message_delta asserts None).",
      "commit_message": "feat(proxy): expose final_output_tokens on PeekResult",
      "done_when": "pytest tests/unit/test_slow_completion_policy.py::test_peek_result_carries_output_tokens tests/unit/test_slow_completion_policy.py::test_peek_result_output_tokens_none_on_absent_delta passes",
      "unit_tests": [
        "tests/unit/test_slow_completion_policy.py::test_peek_result_carries_output_tokens",
        "tests/unit/test_slow_completion_policy.py::test_peek_result_output_tokens_none_on_absent_delta"
      ]
    },
    {
      "index": 2,
      "title": "Add is_slow_completion pure-policy function",
      "files": [
        "src/repoach/llm_proxy/routing/slow_policy.py",
        "src/repoach/llm_proxy/routing/__init__.py",
        "tests/unit/test_slow_completion_policy.py"
      ],
      "action": "Create src/repoach/llm_proxy/routing/slow_policy.py with module docstring stating live-dispatch-vs-offline slowness divergence. Implement is_slow_completion(latency_s, output_tokens, *, gate_s, tps_floor) -> bool returning True iff latency_s > gate_s AND output_tokens is not None AND output_tokens / latency_s < tps_floor. Export is_slow_completion from __init__.py. In test_slow_completion_policy.py add test_is_slow_below_gate_returns_false, test_is_slow_slow_when_above_gate_and_low_tps, test_is_slow_none_tokens_returns_false, test_is_slow_boundaries, test_is_slow_never_raises.",
      "commit_message": "feat(breaker): add is_slow_completion pure policy",
      "done_when": "pytest tests/unit/test_slow_completion_policy.py::test_is_slow_below_gate_returns_false tests/unit/test_slow_completion_policy.py::test_is_slow_slow_when_above_gate_and_low_tps tests/unit/test_slow_completion_policy.py::test_is_slow_none_tokens_returns_false tests/unit/test_slow_completion_policy.py::test_is_slow_boundaries tests/unit/test_slow_completion_policy.py::test_is_slow_never_raises passes",
      "unit_tests": [
        "tests/unit/test_slow_completion_policy.py::test_is_slow_below_gate_returns_false",
        "tests/unit/test_slow_completion_policy.py::test_is_slow_slow_when_above_gate_and_low_tps",
        "tests/unit/test_slow_completion_policy.py::test_is_slow_none_tokens_returns_false",
        "tests/unit/test_slow_completion_policy.py::test_is_slow_boundaries",
        "tests/unit/test_slow_completion_policy.py::test_is_slow_never_raises"
      ]
    },
    {
      "index": 3,
      "title": "Add slow-success k-of-n history to BreakerState",
      "files": [
        "src/repoach/llm_proxy/routing/breaker.py",
        "tests/unit/test_health_breaker.py"
      ],
      "action": "In breaker.py, add _slow_history: dict[ModelRef, list[bool]] to BreakerState.__init__. Implement record_success(ref, slow, *, k, n) -> bool that appends slow, truncates to last n, and returns True if at least k True. Implement trip_slow(ref, *, now, ttl_s, reason='slow_completion') setting _down_until[ref] and _down_reason[ref] without touching _consecutive_failures. Extend recover() to pop ref from _slow_history; ensure down_refs() does not prune slow history; extend clear(). In test_health_breaker.py add test_record_success_k_of_n_window, test_record_success_below_k_returns_false, test_recover_clears_slow_history, test_trip_slow_behavior, test_slow_history_survives_down_refs_prune.",
      "commit_message": "feat(breaker): add slow-success k-of-n history and trip_slow",
      "done_when": "pytest tests/unit/test_health_breaker.py -k 'test_record_success or test_recover_clears_slow_history or test_trip_slow_behavior or test_slow_history_survives_down_refs_prune' passes",
      "unit_tests": [
        "tests/unit/test_health_breaker.py::test_record_success_k_of_n_window",
        "tests/unit/test_health_breaker.py::test_record_success_below_k_returns_false",
        "tests/unit/test_health_breaker.py::test_recover_clears_slow_history",
        "tests/unit/test_health_breaker.py::test_trip_slow_behavior",
        "tests/unit/test_health_breaker.py::test_slow_history_survives_down_refs_prune"
      ]
    },
    {
      "index": 4,
      "title": "Add breaker_slow_* settings to Settings model",
      "files": [
        "src/repoach/llm_proxy/config/settings.py",
        "tests/unit/test_slow_completion_policy.py"
      ],
      "action": "In settings.py, after existing breaker fields add breaker_slow_latency_gate_s: float = 10.0, breaker_slow_tps_floor: float = 1.0, breaker_slow_k: int = 3, breaker_slow_n: int = 5, breaker_slow_ttl_s: float = 300.0, breaker_slow_shadow: bool = True, each declared with validation_alias=_aliases('BREAKER_SLOW_<NAME>') and a matching entry added to _LEGACY_TO_REPOACH_ALIAS, exactly like the existing breaker knobs (BREAKER_TTL_S et al.). In test_slow_completion_policy.py add test_slow_settings_defaults that instantiates Settings and asserts each slow-default value.",
      "commit_message": "feat(config): add breaker_slow_* settings",
      "done_when": "pytest tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults passes",
      "unit_tests": [
        "tests/unit/test_slow_completion_policy.py::test_slow_settings_defaults"
      ]
    },
    {
      "index": 5,
      "title": "Apply slow-completion policy hook in services.py (primary and budget-retry paths)",
      "files": [
        "src/repoach/llm_proxy/api/services.py",
        "tests/unit/test_slow_breaker_wiring.py"
      ],
      "action": "In services.py, import is_slow_completion from routing.slow_policy and the slow settings from config. At the primary success hook (after current recover call), add logic: compute slow = is_slow_completion(attempt_latency_s, peek.final_output_tokens, gate_s=settings.breaker_slow_latency_gate_s, tps_floor=settings.breaker_slow_tps_floor); call breaker.record_success(...) and branch on shadow vs. enforcement. At the budget-retry success point (after retry_peek success), compute retry latency as time.monotonic() - attempt_started and use retry_peek.final_output_tokens; apply the same slow/recover logic. Create tests/unit/test_slow_breaker_wiring.py with test_slow_policy_hook_shadow_mode, test_slow_policy_hook_enforcing_mode, test_slow_policy_below_k_no_trip, test_slow_policy_fast_success_recovers driving the REAL is_slow_completion and a REAL BreakerState — fake only the provider/stream boundary (controlled-latency fake streams carrying chosen output_tokens) and the clock where needed; never replace repoach functions.",
      "commit_message": "feat(proxy): apply slow-completion breaker policy in success paths",
      "done_when": "pytest tests/unit/test_slow_breaker_wiring.py passes",
      "unit_tests": [
        "tests/unit/test_slow_breaker_wiring.py::test_slow_policy_hook_shadow_mode",
        "tests/unit/test_slow_breaker_wiring.py::test_slow_policy_hook_enforcing_mode",
        "tests/unit/test_slow_breaker_wiring.py::test_slow_policy_below_k_no_trip",
        "tests/unit/test_slow_breaker_wiring.py::test_slow_policy_fast_success_recovers"
      ]
    },
    {
      "index": 6,
      "title": "Integration test for slow-completion breaker end-to-end",
      "files": [
        "tests/integration/test_slow_breaker.py"
      ],
      "action": "Create tests/integration/test_slow_breaker.py following the truthful-boundary-fake pattern of test_proxy_dead_hop_quarantine.py. Implement test_slow_breaker_integration_shadow_mode that verifies shadow logging without tripping, and test_slow_breaker_integration_enforcing_mode that verifies a slow_completion trip in /health after k slow completions with shrunk gate.",
      "commit_message": "test(proxy): add integration test for slow-completion breaker",
      "done_when": "pytest tests/integration/test_slow_breaker.py passes",
      "unit_tests": [
        "tests/integration/test_slow_breaker.py::test_slow_breaker_integration_shadow_mode",
        "tests/integration/test_slow_breaker.py::test_slow_breaker_integration_enforcing_mode"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_slow_breaker.py::test_slow_breaker_integration_shadow_mode",
    "tests/integration/test_slow_breaker.py::test_slow_breaker_integration_enforcing_mode"
  ]
}
```
