# SP-PROXY-THINKING-BUDGET-RETRY — Retry a budget-starved candidate with more tokens before failing over

Extend the chain-walk in ClaudeProxyService to detect when a candidate returned an empty completion because its thinking budget was exhausted (not because the provider is dead), and retry that same candidate once with an enlarged max_tokens before advancing to the next chain entry. Three coordinated changes: (1) PeekResult gains a looks_budget_starved flag and an output_tokens field so the caller can distinguish starvation from transport failure; (2) _stream_with_failover reads those flags and, when enabled by settings, re-issues the request with min(max(original * factor, floor), cap) tokens, logging a proxy_budget_retry event; (3) Settings grows four FEROVA_PROXY_BUDGET_RETRY_* knobs. The happy path (candidate yields content on the first attempt) is completely untouched.

## Step 1 — Extend PeekResult with budget-starvation signal

- **Files**: `src/ferova/llm_proxy/api/_failover.py`, `tests/unit/test_proxy_budget_retry.py`
- **Action**: Add two new fields to PeekResult (frozen dataclass, slots=True): `output_tokens: int | None = None` (the final message_delta output_tokens value, or None when no message_delta was seen) and `looks_budget_starved: bool = False`. Set looks_budget_starved=True inside peek_for_content when ALL of: (a) got_content is False, (b) the stream completed normally (stream_done is True), (c) the failure reason is NOT stop_reason_error (i.e. decision_reason in {"output_tokens_zero", "whitespace_only"}), and (d) the NIM disguised-error text is not present — concretely, the accumulated text must NOT match the existing fake-error pattern ("Connection error." prefix already caught by output_tokens==0, so the condition is: decision_reason != "stop_reason_error" and stream_done). Also populate output_tokens=final_output_tokens on every PeekResult. Do NOT change got_content semantics or any existing field. Add unit tests in tests/unit/test_proxy_budget_retry.py covering: peek produces looks_budget_starved=True for a stream with output_tokens=0 and stream_done=True; peek produces looks_budget_starved=False for a stream terminated by stop_reason=error; peek produces looks_budget_starved=False when stream_done=False (transport error path, where stream_done stays False).
- **Commit**: `feat(proxy): classify budget-starved empty completions in PeekResult`
- **Done when**: pytest tests/unit/test_proxy_budget_retry.py::test_budget_starved_peek_sets_flag tests/unit/test_proxy_budget_retry.py::test_error_stop_reason_not_starved tests/unit/test_proxy_budget_retry.py::test_incomplete_stream_not_starved passes and pytest tests/unit/test_proxy_chain_failover.py passes (no regression on existing peek logic)
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py::test_budget_starved_peek_sets_flag`, `tests/unit/test_proxy_budget_retry.py::test_error_stop_reason_not_starved`, `tests/unit/test_proxy_budget_retry.py::test_incomplete_stream_not_starved`

## Step 2 — Add budget-retry knobs to Settings

- **Files**: `src/ferova/llm_proxy/config/settings.py`, `tests/unit/test_proxy_budget_retry.py`
- **Action**: Add four new fields to the Settings class, each using a plain Field (no legacy alias needed — these are new FEROVA_PROXY_* names only): `budget_retry_enabled: bool = Field(default=True, validation_alias='FEROVA_PROXY_BUDGET_RETRY_ENABLED')`, `budget_retry_factor: int = Field(default=8, validation_alias='FEROVA_PROXY_BUDGET_RETRY_FACTOR')`, `budget_retry_floor: int = Field(default=512, validation_alias='FEROVA_PROXY_BUDGET_RETRY_FLOOR')`, `budget_retry_cap: int = Field(default=4096, validation_alias='FEROVA_PROXY_BUDGET_RETRY_CAP')`. Add unit tests in tests/unit/test_proxy_budget_retry.py: test that defaults load correctly via Settings() with no env vars set; test that FEROVA_PROXY_BUDGET_RETRY_ENABLED=false disables the flag; test that factor/floor/cap read from env.
- **Commit**: `feat(proxy): add budget_retry_* knobs to Settings`
- **Done when**: pytest tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_defaults tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_env_override passes and python -c "from ferova.llm_proxy.config.settings import Settings; s=Settings(); assert s.budget_retry_enabled is True" exits 0
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_defaults`, `tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_env_override`

## Step 3 — Implement same-candidate budget retry in _stream_with_failover and add full test suite

- **Files**: `src/ferova/llm_proxy/api/services.py`, `tests/unit/test_proxy_budget_retry.py`
- **Action**: In _stream_with_failover, after the `if peek.got_content:` block and before appending to prior_failures, add: when settings.budget_retry_enabled is True AND peek.got_content is False AND peek.looks_budget_starved is True, compute enlarged = min(max(original_max_tokens * settings.budget_retry_factor, settings.budget_retry_floor), settings.budget_retry_cap) where original_max_tokens = original_request.max_tokens (cast to int, default 0 if None), re-issue the same candidate with attempt_request_retry = original_request.model_copy(update={"model": candidate.provider_model, "max_tokens": enlarged}, deep=True), obtain retry_stream and retry_peek = await peek_for_content(retry_stream) (wrapped in the same exception handler pattern as the primary attempt — on exception treat as empty and fall through to failover), emit a proxy_budget_retry structured log at info level with fields: dispatch_id, request_id, candidate=candidate.provider_model_ref, original_max_tokens, enlarged_max_tokens=enlarged, outcome="content" if retry_peek.got_content else "empty". If retry_peek.got_content, yield buffered chunks and return (success). Otherwise fall through to the existing prior_failures.append and failover log. When budget_retry_enabled is False, skip entirely. The existing exception-catch block around the primary stream call must NOT be extended to wrap the retry — handle the retry exception in its own inner try/except within the starvation branch so a retry exception also just falls through to failover. Add the four pinned tests from the spec DoD to tests/unit/test_proxy_budget_retry.py using the _ScriptedProvider + _build_service pattern from test_proxy_chain_failover.py, plus a _ScriptedProviderThreshold variant whose stream_response returns empty chunks when called with max_tokens below a threshold and real chunks at or above it (inspect request.max_tokens in stream_response). The four tests: test_budget_starved_empty_retries_same_candidate_with_more_budget, test_dead_candidate_fails_over_after_one_retry, test_non_starved_empty_does_not_retry, test_disabled_flag_keeps_immediate_failover.
- **Commit**: `test(proxy): budget-retry — starved retries, dead fails over, disabled is inert`
- **Done when**: pytest tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry tests/unit/test_proxy_budget_retry.py::test_non_starved_empty_does_not_retry tests/unit/test_proxy_budget_retry.py::test_disabled_flag_keeps_immediate_failover passes and pytest tests/unit/ passes and ruff check src/ferova/llm_proxy/api/_failover.py src/ferova/llm_proxy/api/services.py src/ferova/llm_proxy/config/settings.py exits 0
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget`, `tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry`, `tests/unit/test_proxy_budget_retry.py::test_non_starved_empty_does_not_retry`, `tests/unit/test_proxy_budget_retry.py::test_disabled_flag_keeps_immediate_failover`

## Integration tests

- `tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget`
- `tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PROXY-THINKING-BUDGET-RETRY",
  "title": "Retry a budget-starved candidate with more tokens before failing over",
  "summary": "Extend the chain-walk in ClaudeProxyService to detect when a candidate returned an empty completion because its thinking budget was exhausted (not because the provider is dead), and retry that same candidate once with an enlarged max_tokens before advancing to the next chain entry. Three coordinated changes: (1) PeekResult gains a looks_budget_starved flag and an output_tokens field so the caller can distinguish starvation from transport failure; (2) _stream_with_failover reads those flags and, when enabled by settings, re-issues the request with min(max(original * factor, floor), cap) tokens, logging a proxy_budget_retry event; (3) Settings grows four FEROVA_PROXY_BUDGET_RETRY_* knobs. The happy path (candidate yields content on the first attempt) is completely untouched.",
  "steps": [
    {
      "index": 1,
      "title": "Extend PeekResult with budget-starvation signal",
      "files": [
        "src/ferova/llm_proxy/api/_failover.py",
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "Add two new fields to PeekResult (frozen dataclass, slots=True): `output_tokens: int | None = None` (the final message_delta output_tokens value, or None when no message_delta was seen) and `looks_budget_starved: bool = False`. Set looks_budget_starved=True inside peek_for_content when ALL of: (a) got_content is False, (b) the stream completed normally (stream_done is True), (c) the failure reason is NOT stop_reason_error (i.e. decision_reason in {\"output_tokens_zero\", \"whitespace_only\"}), and (d) the NIM disguised-error text is not present — concretely, the accumulated text must NOT match the existing fake-error pattern (\"Connection error.\" prefix already caught by output_tokens==0, so the condition is: decision_reason != \"stop_reason_error\" and stream_done). Also populate output_tokens=final_output_tokens on every PeekResult. Do NOT change got_content semantics or any existing field. Add unit tests in tests/unit/test_proxy_budget_retry.py covering: peek produces looks_budget_starved=True for a stream with output_tokens=0 and stream_done=True; peek produces looks_budget_starved=False for a stream terminated by stop_reason=error; peek produces looks_budget_starved=False when stream_done=False (transport error path, where stream_done stays False).",
      "commit_message": "feat(proxy): classify budget-starved empty completions in PeekResult",
      "done_when": "pytest tests/unit/test_proxy_budget_retry.py::test_budget_starved_peek_sets_flag tests/unit/test_proxy_budget_retry.py::test_error_stop_reason_not_starved tests/unit/test_proxy_budget_retry.py::test_incomplete_stream_not_starved passes and pytest tests/unit/test_proxy_chain_failover.py passes (no regression on existing peek logic)",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py::test_budget_starved_peek_sets_flag",
        "tests/unit/test_proxy_budget_retry.py::test_error_stop_reason_not_starved",
        "tests/unit/test_proxy_budget_retry.py::test_incomplete_stream_not_starved"
      ]
    },
    {
      "index": 2,
      "title": "Add budget-retry knobs to Settings",
      "files": [
        "src/ferova/llm_proxy/config/settings.py",
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "Add four new fields to the Settings class, each using a plain Field (no legacy alias needed — these are new FEROVA_PROXY_* names only): `budget_retry_enabled: bool = Field(default=True, validation_alias='FEROVA_PROXY_BUDGET_RETRY_ENABLED')`, `budget_retry_factor: int = Field(default=8, validation_alias='FEROVA_PROXY_BUDGET_RETRY_FACTOR')`, `budget_retry_floor: int = Field(default=512, validation_alias='FEROVA_PROXY_BUDGET_RETRY_FLOOR')`, `budget_retry_cap: int = Field(default=4096, validation_alias='FEROVA_PROXY_BUDGET_RETRY_CAP')`. Add unit tests in tests/unit/test_proxy_budget_retry.py: test that defaults load correctly via Settings() with no env vars set; test that FEROVA_PROXY_BUDGET_RETRY_ENABLED=false disables the flag; test that factor/floor/cap read from env.",
      "commit_message": "feat(proxy): add budget_retry_* knobs to Settings",
      "done_when": "pytest tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_defaults tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_env_override passes and python -c \"from ferova.llm_proxy.config.settings import Settings; s=Settings(); assert s.budget_retry_enabled is True\" exits 0",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_defaults",
        "tests/unit/test_proxy_budget_retry.py::test_settings_budget_retry_env_override"
      ]
    },
    {
      "index": 3,
      "title": "Implement same-candidate budget retry in _stream_with_failover and add full test suite",
      "files": [
        "src/ferova/llm_proxy/api/services.py",
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "In _stream_with_failover, after the `if peek.got_content:` block and before appending to prior_failures, add: when settings.budget_retry_enabled is True AND peek.got_content is False AND peek.looks_budget_starved is True, compute enlarged = min(max(original_max_tokens * settings.budget_retry_factor, settings.budget_retry_floor), settings.budget_retry_cap) where original_max_tokens = original_request.max_tokens (cast to int, default 0 if None), re-issue the same candidate with attempt_request_retry = original_request.model_copy(update={\"model\": candidate.provider_model, \"max_tokens\": enlarged}, deep=True), obtain retry_stream and retry_peek = await peek_for_content(retry_stream) (wrapped in the same exception handler pattern as the primary attempt — on exception treat as empty and fall through to failover), emit a proxy_budget_retry structured log at info level with fields: dispatch_id, request_id, candidate=candidate.provider_model_ref, original_max_tokens, enlarged_max_tokens=enlarged, outcome=\"content\" if retry_peek.got_content else \"empty\". If retry_peek.got_content, yield buffered chunks and return (success). Otherwise fall through to the existing prior_failures.append and failover log. When budget_retry_enabled is False, skip entirely. The existing exception-catch block around the primary stream call must NOT be extended to wrap the retry — handle the retry exception in its own inner try/except within the starvation branch so a retry exception also just falls through to failover. Add the four pinned tests from the spec DoD to tests/unit/test_proxy_budget_retry.py using the _ScriptedProvider + _build_service pattern from test_proxy_chain_failover.py, plus a _ScriptedProviderThreshold variant whose stream_response returns empty chunks when called with max_tokens below a threshold and real chunks at or above it (inspect request.max_tokens in stream_response). The four tests: test_budget_starved_empty_retries_same_candidate_with_more_budget, test_dead_candidate_fails_over_after_one_retry, test_non_starved_empty_does_not_retry, test_disabled_flag_keeps_immediate_failover.",
      "commit_message": "test(proxy): budget-retry — starved retries, dead fails over, disabled is inert",
      "done_when": "pytest tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry tests/unit/test_proxy_budget_retry.py::test_non_starved_empty_does_not_retry tests/unit/test_proxy_budget_retry.py::test_disabled_flag_keeps_immediate_failover passes and pytest tests/unit/ passes and ruff check src/ferova/llm_proxy/api/_failover.py src/ferova/llm_proxy/api/services.py src/ferova/llm_proxy/config/settings.py exits 0",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget",
        "tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry",
        "tests/unit/test_proxy_budget_retry.py::test_non_starved_empty_does_not_retry",
        "tests/unit/test_proxy_budget_retry.py::test_disabled_flag_keeps_immediate_failover"
      ]
    }
  ],
  "integration_tests": [
    "tests/unit/test_proxy_budget_retry.py::test_budget_starved_empty_retries_same_candidate_with_more_budget",
    "tests/unit/test_proxy_budget_retry.py::test_dead_candidate_fails_over_after_one_retry"
  ]
}
```
