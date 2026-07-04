# SP-BUDGET-RETRY-FIXES — Budget retry — None guard and real escalation headroom

One step: both fixes land with their promised AC tests so every promise discriminates the new work (the generated plan's bare-file promises preflighted vacuously green).

## Step 1 — Guard None max_tokens, enforce real escalation, raise the cap default

- **Files**: `src/ferova/llm_proxy/config/settings.py`, `src/ferova/llm_proxy/api/services.py`, `tests/unit/test_proxy_budget_retry.py`
- **Action**: In src/ferova/llm_proxy/api/services.py, make _retry_with_more_budget handle a None original max_tokens (base the escalation on the provider-effective default that was actually in play instead of multiplying None), and make the escalated ask strictly exceed the effective post-floor tokens of the first attempt or skip the retry with the existing at-cap semantics. In src/ferova/llm_proxy/config/settings.py, raise the budget_retry_cap default from 4096 to 8192 so the x8 factor has room above the 4096 answer-headroom floor of the combined-budget providers. In tests/unit/test_proxy_budget_retry.py, add the three promised tests and update any test pinning the old 4096 default. Existing pinned behaviours stay: one escalation per candidate, no carry-over to the next candidate, disabled-flag passthrough, non-starved empties never retried.
- **Commit**: `fix(llm_proxy): budget retry None guard and real escalation headroom`
- **Done when**: the three promised selectors pass and the whole budget-retry suite is green
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py::test_none_max_tokens_starved_empty_does_not_crash`, `tests/unit/test_proxy_budget_retry.py::test_escalation_exceeds_the_effective_floor`, `tests/unit/test_proxy_budget_retry.py::test_at_cap_requests_still_fail_over_without_retry`

## Integration tests

- `tests/unit/test_proxy_budget_retry.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-BUDGET-RETRY-FIXES",
  "title": "Budget retry — None guard and real escalation headroom",
  "summary": "One step: both fixes land with their promised AC tests so every promise discriminates the new work (the generated plan's bare-file promises preflighted vacuously green).",
  "steps": [
    {
      "index": 1,
      "title": "Guard None max_tokens, enforce real escalation, raise the cap default",
      "files": [
        "src/ferova/llm_proxy/config/settings.py",
        "src/ferova/llm_proxy/api/services.py",
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "In src/ferova/llm_proxy/api/services.py, make _retry_with_more_budget handle a None original max_tokens (base the escalation on the provider-effective default that was actually in play instead of multiplying None), and make the escalated ask strictly exceed the effective post-floor tokens of the first attempt or skip the retry with the existing at-cap semantics. In src/ferova/llm_proxy/config/settings.py, raise the budget_retry_cap default from 4096 to 8192 so the x8 factor has room above the 4096 answer-headroom floor of the combined-budget providers. In tests/unit/test_proxy_budget_retry.py, add the three promised tests and update any test pinning the old 4096 default. Existing pinned behaviours stay: one escalation per candidate, no carry-over to the next candidate, disabled-flag passthrough, non-starved empties never retried.",
      "commit_message": "fix(llm_proxy): budget retry None guard and real escalation headroom",
      "done_when": "the three promised selectors pass and the whole budget-retry suite is green",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py::test_none_max_tokens_starved_empty_does_not_crash",
        "tests/unit/test_proxy_budget_retry.py::test_escalation_exceeds_the_effective_floor",
        "tests/unit/test_proxy_budget_retry.py::test_at_cap_requests_still_fail_over_without_retry"
      ]
    }
  ],
  "integration_tests": [
    "tests/unit/test_proxy_budget_retry.py"
  ]
}
```
