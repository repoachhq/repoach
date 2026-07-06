# SP-PLANNER-REFINE-HISTORY — Planner refine loop carries full error history

Stop the refine loop's whack-a-mole by feeding the model the session's full error history (numbered, oldest first, each truncated to 300 chars) instead of only the latest error, and by making the parse-attempt budget a per-session setting (FEROVA_PLANNER_PARSE_ATTEMPTS, default 5) read once at session start. Every rejection log line gains an errors_so_far count so a whack-a-mole session is visible in logs, and the final exhausted-session error names every attempt's failure.

## Step 1 — Carry full error history in refine prompt + setting + log count

- **Files**: `src/ferova/review/planner.py`, `tests/unit/test_review_planner.py`
- **Action**: In src/ferova/review/planner.py: (a) replace the module constant `_PLAN_PARSE_ATTEMPTS = 3` with a per-session setting read from env `FEROVA_PLANNER_PARSE_ATTEMPTS` (default 5, clamped to >=1) computed once at the top of `run_planner_session` and threaded into `Planner.plan` (e.g. via a new `parse_attempts: int` parameter on `Planner.__init__` defaulting to the env-read value, or via a small `_parse_attempts()` helper called at the start of `_plan_via_proxy` / `_plan_via_cc`); the constant's semantics (1 initial + N-1 refinements) are unchanged. (b) Change `_refine_prompt(previous_text, error)` to `_refine_prompt(previous_text, errors: list[str])`; build the prompt so it embeds the rejected candidate (still capped at 6000 chars) followed by a numbered list of every prior error, oldest first, each truncated to 300 chars, under an explicit instruction: 'your next candidate must satisfy ALL of these at once; re-check each before answering.' (c) Update both retry loops (`_plan_via_proxy` and `_plan_via_cc`) to accumulate `errors: list[str]` across attempts, pass the full list to `_refine_prompt`, and include `errors_so_far=len(errors)` in every `planner.plan_invalid` log line. (d) When the budget is exhausted, return an error string that names every attempt's failure (joined, oldest first) instead of only `last_error`. Append four NEW module-level tests to tests/unit/test_review_planner.py (the file already exists): `test_refine_prompt_carries_full_error_history` (after two rejections the third prompt contains both errors, numbered, oldest first), `test_parse_attempts_setting_is_honored` (with env set to 2, a never-valid candidate makes exactly 2 attempts), `test_exhausted_session_reports_full_history` (the final error names every attempt's failure), and `test_single_error_history_matches_previous_behaviour` (one rejection produces a prompt equivalent to today's shape — i.e. contains the single error and the 'REJECTED' marker).
- **Commit**: `feat(planner): carry full error history in refine loop`
- **Done when**: pytest tests/unit/test_review_planner.py -k 'refine_prompt_carries_full_error_history or parse_attempts_setting_is_honored or exhausted_session_reports_full_history or single_error_history_matches_previous_behaviour' passes, and `ruff check src/ferova/review/planner.py` exits 0
- **Unit tests**: `tests/unit/test_review_planner.py::test_refine_prompt_carries_full_error_history`, `tests/unit/test_review_planner.py::test_parse_attempts_setting_is_honored`, `tests/unit/test_review_planner.py::test_exhausted_session_reports_full_history`, `tests/unit/test_review_planner.py::test_single_error_history_matches_previous_behaviour`

## Step 2 — Integration test: two-error session converges with history

- **Files**: `tests/integration/test_planner_refine_history.py`
- **Action**: Create tests/integration/test_planner_refine_history.py (NEW file) modelled on tests/integration/test_planner_selector_check.py: seed a tmp repo with a spec doc, drive `run_planner_session` end to end with a scripted fake AgentLoop that emits a different invalid plan on attempt 1 (e.g. missing commit_message) and attempt 2 (e.g. wrong spec_id), then a valid plan on attempt 3. Assert: (i) the session succeeds (written=True, error is None); (ii) the third prompt (the one that produced the accepted plan) contains BOTH prior errors, numbered, oldest first; (iii) the `planner.plan_invalid` log lines carry an `errors_so_far` count that grows across attempts (use `caplog` against the `ferova.review.planner` logger). Also add a sibling assertion that with FEROVA_PLANNER_PARSE_ATTEMPTS=2 a never-valid candidate makes exactly 2 attempts and the final outcome.error names both failures.
- **Commit**: `test(planner): integration coverage for refine error history`
- **Done when**: pytest tests/integration/test_planner_refine_history.py::test_two_error_session_converges_with_history passes
- **Unit tests**: `tests/integration/test_planner_refine_history.py::test_two_error_session_converges_with_history`

## Integration tests

- `tests/integration/test_planner_refine_history.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-PLANNER-REFINE-HISTORY",
  "title": "Planner refine loop carries full error history",
  "summary": "Stop the refine loop's whack-a-mole by feeding the model the session's full error history (numbered, oldest first, each truncated to 300 chars) instead of only the latest error, and by making the parse-attempt budget a per-session setting (FEROVA_PLANNER_PARSE_ATTEMPTS, default 5) read once at session start. Every rejection log line gains an errors_so_far count so a whack-a-mole session is visible in logs, and the final exhausted-session error names every attempt's failure.",
  "steps": [
    {
      "index": 1,
      "title": "Carry full error history in refine prompt + setting + log count",
      "files": [
        "src/ferova/review/planner.py",
        "tests/unit/test_review_planner.py"
      ],
      "action": "In src/ferova/review/planner.py: (a) replace the module constant `_PLAN_PARSE_ATTEMPTS = 3` with a per-session setting read from env `FEROVA_PLANNER_PARSE_ATTEMPTS` (default 5, clamped to >=1) computed once at the top of `run_planner_session` and threaded into `Planner.plan` (e.g. via a new `parse_attempts: int` parameter on `Planner.__init__` defaulting to the env-read value, or via a small `_parse_attempts()` helper called at the start of `_plan_via_proxy` / `_plan_via_cc`); the constant's semantics (1 initial + N-1 refinements) are unchanged. (b) Change `_refine_prompt(previous_text, error)` to `_refine_prompt(previous_text, errors: list[str])`; build the prompt so it embeds the rejected candidate (still capped at 6000 chars) followed by a numbered list of every prior error, oldest first, each truncated to 300 chars, under an explicit instruction: 'your next candidate must satisfy ALL of these at once; re-check each before answering.' (c) Update both retry loops (`_plan_via_proxy` and `_plan_via_cc`) to accumulate `errors: list[str]` across attempts, pass the full list to `_refine_prompt`, and include `errors_so_far=len(errors)` in every `planner.plan_invalid` log line. (d) When the budget is exhausted, return an error string that names every attempt's failure (joined, oldest first) instead of only `last_error`. Append four NEW module-level tests to tests/unit/test_review_planner.py (the file already exists): `test_refine_prompt_carries_full_error_history` (after two rejections the third prompt contains both errors, numbered, oldest first), `test_parse_attempts_setting_is_honored` (with env set to 2, a never-valid candidate makes exactly 2 attempts), `test_exhausted_session_reports_full_history` (the final error names every attempt's failure), and `test_single_error_history_matches_previous_behaviour` (one rejection produces a prompt equivalent to today's shape — i.e. contains the single error and the 'REJECTED' marker).",
      "commit_message": "feat(planner): carry full error history in refine loop",
      "done_when": "pytest tests/unit/test_review_planner.py -k 'refine_prompt_carries_full_error_history or parse_attempts_setting_is_honored or exhausted_session_reports_full_history or single_error_history_matches_previous_behaviour' passes, and `ruff check src/ferova/review/planner.py` exits 0",
      "unit_tests": [
        "tests/unit/test_review_planner.py::test_refine_prompt_carries_full_error_history",
        "tests/unit/test_review_planner.py::test_parse_attempts_setting_is_honored",
        "tests/unit/test_review_planner.py::test_exhausted_session_reports_full_history",
        "tests/unit/test_review_planner.py::test_single_error_history_matches_previous_behaviour"
      ]
    },
    {
      "index": 2,
      "title": "Integration test: two-error session converges with history",
      "files": [
        "tests/integration/test_planner_refine_history.py"
      ],
      "action": "Create tests/integration/test_planner_refine_history.py (NEW file) modelled on tests/integration/test_planner_selector_check.py: seed a tmp repo with a spec doc, drive `run_planner_session` end to end with a scripted fake AgentLoop that emits a different invalid plan on attempt 1 (e.g. missing commit_message) and attempt 2 (e.g. wrong spec_id), then a valid plan on attempt 3. Assert: (i) the session succeeds (written=True, error is None); (ii) the third prompt (the one that produced the accepted plan) contains BOTH prior errors, numbered, oldest first; (iii) the `planner.plan_invalid` log lines carry an `errors_so_far` count that grows across attempts (use `caplog` against the `ferova.review.planner` logger). Also add a sibling assertion that with FEROVA_PLANNER_PARSE_ATTEMPTS=2 a never-valid candidate makes exactly 2 attempts and the final outcome.error names both failures.",
      "commit_message": "test(planner): integration coverage for refine error history",
      "done_when": "pytest tests/integration/test_planner_refine_history.py::test_two_error_session_converges_with_history passes",
      "unit_tests": [
        "tests/integration/test_planner_refine_history.py::test_two_error_session_converges_with_history"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_planner_refine_history.py"
  ]
}
```
