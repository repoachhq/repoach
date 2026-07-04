# SP-BUDGET-RETRY-FIXES — Budget retry — None guard and real escalation headroom

Fix two defects in the thinking-budget retry path: (1) a TypeError crash when max_tokens is None, and (2) a cap that makes retry a no-op for combined-budget providers by raising the default cap from 4096 to 8192 and ensuring the enlarged value strictly exceeds the effective floor.

## Step 1 — Raise default budget_retry_cap to 8192

- **Files**: `src/ferova/llm_proxy/config/settings.py`, `tests/unit/test_proxy_budget_retry.py`
- **Action**: Change the default value of budget_retry_cap from 4096 to 8192 in the Settings class Field declaration, and update any pinned default assertion in the budget-retry test suite from 4096 to 8192.
- **Commit**: `fix(settings): raise default budget_retry_cap from 4096 to 8192`
- **Done when**: grep -q 'budget_retry_cap: int = Field(default=8192' src/ferova/llm_proxy/config/settings.py
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py`

## Step 2 — Guard None max_tokens and enforce real escalation in retry

- **Files**: `src/ferova/llm_proxy/api/services.py`
- **Action**: In _retry_with_more_budget, handle None max_tokens by treating it as the effective floor (4096 for combined-budget providers). After computing the enlarged value, ensure it strictly exceeds the effective floor; if not, skip the retry. Update the enlarged value computation to use the effective base when original_max is None.
- **Commit**: `fix(proxy): guard None max_tokens and enforce real escalation in budget retry`
- **Done when**: pytest tests/unit/test_proxy_budget_retry.py -x --tb=short passes
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py`

## Step 3 — Add new budget retry tests and verify full suite

- **Files**: `tests/unit/test_proxy_budget_retry.py`
- **Action**: Add test_none_max_tokens_starved_empty_does_not_crash (AC1), test_escalation_exceeds_the_effective_floor (AC2), and test_at_cap_requests_still_fail_over_without_retry (AC3). Then run the full unit suite to confirm no regressions.
- **Commit**: `test(proxy): pin None-guard and real-escalation behaviour for budget retry`
- **Done when**: pytest tests/unit -x --tb=short passes
- **Unit tests**: `tests/unit/test_proxy_budget_retry.py::test_none_max_tokens_starved_empty_does_not_crash`, `tests/unit/test_proxy_budget_retry.py::test_escalation_exceeds_the_effective_floor`, `tests/unit/test_proxy_budget_retry.py::test_at_cap_requests_still_fail_over_without_retry`

## Integration tests

- `tests/unit/test_proxy_budget_retry.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-BUDGET-RETRY-FIXES",
  "title": "Budget retry — None guard and real escalation headroom",
  "summary": "Fix two defects in the thinking-budget retry path: (1) a TypeError crash when max_tokens is None, and (2) a cap that makes retry a no-op for combined-budget providers by raising the default cap from 4096 to 8192 and ensuring the enlarged value strictly exceeds the effective floor.",
  "steps": [
    {
      "index": 1,
      "title": "Raise default budget_retry_cap to 8192",
      "files": [
        "src/ferova/llm_proxy/config/settings.py",
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "Change the default value of budget_retry_cap from 4096 to 8192 in the Settings class Field declaration, and update any pinned default assertion in the budget-retry test suite from 4096 to 8192.",
      "commit_message": "fix(settings): raise default budget_retry_cap from 4096 to 8192",
      "done_when": "grep -q 'budget_retry_cap: int = Field(default=8192' src/ferova/llm_proxy/config/settings.py",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py"
      ]
    },
    {
      "index": 2,
      "title": "Guard None max_tokens and enforce real escalation in retry",
      "files": [
        "src/ferova/llm_proxy/api/services.py"
      ],
      "action": "In _retry_with_more_budget, handle None max_tokens by treating it as the effective floor (4096 for combined-budget providers). After computing the enlarged value, ensure it strictly exceeds the effective floor; if not, skip the retry. Update the enlarged value computation to use the effective base when original_max is None.",
      "commit_message": "fix(proxy): guard None max_tokens and enforce real escalation in budget retry",
      "done_when": "pytest tests/unit/test_proxy_budget_retry.py -x --tb=short passes",
      "unit_tests": [
        "tests/unit/test_proxy_budget_retry.py"
      ]
    },
    {
      "index": 3,
      "title": "Add new budget retry tests and verify full suite",
      "files": [
        "tests/unit/test_proxy_budget_retry.py"
      ],
      "action": "Add test_none_max_tokens_starved_empty_does_not_crash (AC1), test_escalation_exceeds_the_effective_floor (AC2), and test_at_cap_requests_still_fail_over_without_retry (AC3). Then run the full unit suite to confirm no regressions.",
      "commit_message": "test(proxy): pin None-guard and real-escalation behaviour for budget retry",
      "done_when": "pytest tests/unit -x --tb=short passes",
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
