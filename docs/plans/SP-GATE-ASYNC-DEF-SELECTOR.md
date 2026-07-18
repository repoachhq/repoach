# SP-GATE-ASYNC-DEF-SELECTOR — Widen promise-presence predicates to recognise async def tests

Three regex sites in the review subsystem match only `def NAME(` and miss `async def` tests. Widen each to `(?:async\s+)?def`, update docstrings, and add unit + integration coverage proving async promises satisfy the merge gate.

## Step 1 — Widen promised_present and test_function_names regexes for async def

- **Files**: `src/ferova/review/spec_gate.py`, `src/ferova/review/dev_runner.py`, `tests/unit/test_promise_async_def.py`
- **Action**: In spec_gate.py, change the pattern in promised_present from r"(?m)^\s*def\s+" to r"(?m)^\s*(?:async\s+)?def\s+" and update the docstring quotes that show the old pattern. In dev_runner.py, change the pattern in _test_function_names_in_file from r"(?m)^\s*def\s+(test_\w+)\s*\(" to r"(?m)^\s*(?:async\s+)?def\s+(test_\w+)\s*\(". Create tests/unit/test_promise_async_def.py with four tmp_path-based tests: test_promised_present_matches_async_def, test_promised_present_async_def_class_scoped, test_promised_present_async_name_boundary, test_test_function_names_lists_async_defs. Each test writes real .py files and calls the public functions directly.
- **Commit**: `fix(review): widen promise predicates to accept async def tests`
- **Done when**: pytest tests/unit/test_promise_async_def.py -v passes and ruff check src/ferova/review/spec_gate.py src/ferova/review/dev_runner.py exits 0
- **Unit tests**: `tests/unit/test_promise_async_def.py::test_promised_present_matches_async_def`, `tests/unit/test_promise_async_def.py::test_promised_present_async_def_class_scoped`, `tests/unit/test_promise_async_def.py::test_promised_present_async_name_boundary`, `tests/unit/test_promise_async_def.py::test_test_function_names_lists_async_defs`

## Step 2 — Widen placeholder guard regex and add integration coverage test

- **Files**: `src/ferova/review/coder_loop.py`, `tests/unit/test_promise_async_def.py`, `tests/integration/test_gate_async_def_coverage.py`
- **Action**: In coder_loop.py, change the placeholder-guard regex in is_placeholder_content from r"^\s*def\s+test_\w+" to r"^\s*(?:async\s+)?def\s+test_\w+". Add test_async_only_test_file_not_placeholder to the existing tests/unit/test_promise_async_def.py: is_placeholder_content on a new tests/ path whose content defines only async def test_* functions returns is_placeholder=False. Create tests/integration/test_gate_async_def_coverage.py with test_async_promises_yield_covered_and_gate_reason_free: build a tmp repo tree delivering async def tests, call compute_spec_coverage for a plan promising those selectors, assert covered=True / missing=[], record_spec_coverage into a tmp SQLite DB, then gather_merge_facts + compute_merge_decision and assert the decision's reasons do NOT contain "spec acceptance selectors not all present".
- **Commit**: `fix(review): widen placeholder guard and add async coverage integration test`
- **Done when**: pytest tests/unit/test_promise_async_def.py tests/integration/test_gate_async_def_coverage.py -v passes and ruff check src/ferova/review/coder_loop.py exits 0
- **Unit tests**: `tests/unit/test_promise_async_def.py::test_async_only_test_file_not_placeholder`

## Integration tests

- `tests/integration/test_gate_async_def_coverage.py::test_async_promises_yield_covered_and_gate_reason_free`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-GATE-ASYNC-DEF-SELECTOR",
  "title": "Widen promise-presence predicates to recognise async def tests",
  "summary": "Three regex sites in the review subsystem match only `def NAME(` and miss `async def` tests. Widen each to `(?:async\\s+)?def`, update docstrings, and add unit + integration coverage proving async promises satisfy the merge gate.",
  "steps": [
    {
      "index": 1,
      "title": "Widen promised_present and test_function_names regexes for async def",
      "files": [
        "src/ferova/review/spec_gate.py",
        "src/ferova/review/dev_runner.py",
        "tests/unit/test_promise_async_def.py"
      ],
      "action": "In spec_gate.py, change the pattern in promised_present from r\"(?m)^\\s*def\\s+\" to r\"(?m)^\\s*(?:async\\s+)?def\\s+\" and update the docstring quotes that show the old pattern. In dev_runner.py, change the pattern in _test_function_names_in_file from r\"(?m)^\\s*def\\s+(test_\\w+)\\s*\\(\" to r\"(?m)^\\s*(?:async\\s+)?def\\s+(test_\\w+)\\s*\\(\". Create tests/unit/test_promise_async_def.py with four tmp_path-based tests: test_promised_present_matches_async_def, test_promised_present_async_def_class_scoped, test_promised_present_async_name_boundary, test_test_function_names_lists_async_defs. Each test writes real .py files and calls the public functions directly.",
      "commit_message": "fix(review): widen promise predicates to accept async def tests",
      "done_when": "pytest tests/unit/test_promise_async_def.py -v passes and ruff check src/ferova/review/spec_gate.py src/ferova/review/dev_runner.py exits 0",
      "unit_tests": [
        "tests/unit/test_promise_async_def.py::test_promised_present_matches_async_def",
        "tests/unit/test_promise_async_def.py::test_promised_present_async_def_class_scoped",
        "tests/unit/test_promise_async_def.py::test_promised_present_async_name_boundary",
        "tests/unit/test_promise_async_def.py::test_test_function_names_lists_async_defs"
      ]
    },
    {
      "index": 2,
      "title": "Widen placeholder guard regex and add integration coverage test",
      "files": [
        "src/ferova/review/coder_loop.py",
        "tests/unit/test_promise_async_def.py",
        "tests/integration/test_gate_async_def_coverage.py"
      ],
      "action": "In coder_loop.py, change the placeholder-guard regex in is_placeholder_content from r\"^\\s*def\\s+test_\\w+\" to r\"^\\s*(?:async\\s+)?def\\s+test_\\w+\". Add test_async_only_test_file_not_placeholder to the existing tests/unit/test_promise_async_def.py: is_placeholder_content on a new tests/ path whose content defines only async def test_* functions returns is_placeholder=False. Create tests/integration/test_gate_async_def_coverage.py with test_async_promises_yield_covered_and_gate_reason_free: build a tmp repo tree delivering async def tests, call compute_spec_coverage for a plan promising those selectors, assert covered=True / missing=[], record_spec_coverage into a tmp SQLite DB, then gather_merge_facts + compute_merge_decision and assert the decision's reasons do NOT contain \"spec acceptance selectors not all present\".",
      "commit_message": "fix(review): widen placeholder guard and add async coverage integration test",
      "done_when": "pytest tests/unit/test_promise_async_def.py tests/integration/test_gate_async_def_coverage.py -v passes and ruff check src/ferova/review/coder_loop.py exits 0",
      "unit_tests": [
        "tests/unit/test_promise_async_def.py::test_async_only_test_file_not_placeholder"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_gate_async_def_coverage.py::test_async_promises_yield_covered_and_gate_reason_free"
  ]
}
```
