# SP-AUTOMERGE-EVENT-DRIVEN — Lane 1: settings-sourced CI-gate wait with a fail-fast zero contract

Hand-authored plan (Planner exhausted its 5 parse attempts on selector-creation phrasing; telemetry rows 978-982). Scope is Lane 1 only: G1-G3 of the spec. Lane 2 (G4-G6, `.github/workflows/*`, `.env.example`) is operator-manual and MUST NOT appear in any step.

## Step 1 — Settings gain the two automerge CI-gate knobs

- **Files**: `src/ferova/core/config.py`, `tests/unit/test_automerge_fail_fast_gate.py`
- **Action**: In the Settings class in config.py, add two fields following the module's existing Field pattern (env_prefix supplies the FEROVA_ name): automerge_ci_wait_seconds: int = Field(default=720, ge=0, description="Total CI-gate wait budget in seconds; 0 = single evaluation, fail fast.") and automerge_ci_poll_interval: int = Field(default=30, ge=1, description="Seconds between CI rollup polls when the budget allows waiting."). Create tests/unit/test_automerge_fail_fast_gate.py with one test test_settings_env_overrides_wait_and_poll that: builds Settings(_env_file=None) with FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 and FEROVA_AUTOMERGE_CI_POLL_INTERVAL=5 set in the environment and asserts the fields are 0 and 5; builds Settings(_env_file=None) with both vars absent and asserts 720 and 30; asserts pydantic.ValidationError is raised for FEROVA_AUTOMERGE_CI_WAIT_SECONDS=-1 and for FEROVA_AUTOMERGE_CI_POLL_INTERVAL=0. Always pass _env_file=None so the test is immune to env-file anchoring changes.
- **Commit**: `feat(config): automerge CI-gate wait/poll settings`
- **Done when**: pytest tests/unit/test_automerge_fail_fast_gate.py tests/unit/test_settings_chains_env_precedence.py tests/unit/test_settings_sharp_prefix_aliases.py -x -q passes; ruff check src/ferova/core/config.py tests/unit/test_automerge_fail_fast_gate.py exits 0
- **Unit tests**: `tests/unit/test_automerge_fail_fast_gate.py::test_settings_env_overrides_wait_and_poll`

## Step 2 — auto_merge sources wait/poll defaults from settings; fail-fast zero contract

- **Files**: `src/ferova/review/auto_merge.py`, `tests/unit/test_automerge_fail_fast_gate.py`
- **Action**: In auto_merge.py change the wait_seconds and poll_interval parameters of evaluate_ci_gate, required_checks_green, evaluate_merge_gate and run_auto_merge from int defaults to int | None = None, and resolve them at call time through a new private helper _resolve_wait_poll(wait_seconds: int | None, poll_interval: int | None) -> tuple[int, int] that returns the argument when not None and otherwise the value from ferova.core.config.get_settings() (automerge_ci_wait_seconds / automerge_ci_poll_interval). Delete the module constants _DEFAULT_WAIT_SECONDS and _DEFAULT_POLL_INTERVAL. decide_at_head is deliberately untouched (it consumes an already-computed ci_green). Update the docstrings that describe the retired 12-minute window: the module docstring bullet about _DEFAULT_WAIT_SECONDS matching the auto-review.yml wait, and the evaluate_ci_gate docstring line citing SP-AUTOMERGE-CI-GATE, both now state that defaults come from Settings (FEROVA_AUTOMERGE_CI_WAIT_SECONDS / FEROVA_AUTOMERGE_CI_POLL_INTERVAL) and that wait_seconds=0 means exactly one rollup evaluation with zero sleeps (SP-AUTOMERGE-EVENT-DRIVEN). Append to tests/unit/test_automerge_fail_fast_gate.py four tests: test_wait_zero_single_evaluation_no_sleep (evaluate_ci_gate with wait_seconds=0 against a truthful boundary-fake GhCli whose rollup keeps one required check QUEUED, an injected sleep recorder and injected monotonic: exactly one rollup read, recorder never called, outcome SKIP_CI_TIMEOUT, reason names the pending check), test_run_auto_merge_wait_zero_persists_fail_fast_skip (run_auto_merge with wait_seconds=0, fake GhCli with a pending required check, real sqlite db_path in tmp_path: persists a pr_merges row with outcome SKIP_CI_TIMEOUT and the merge endpoint is never called — reuse the fixture pattern of tests/unit/test_review_auto_merge.py), test_explicit_wait_argument_beats_settings (set FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 in the environment and reset the cached settings singleton by assigning ferova.core.config._settings = None, then call evaluate_ci_gate with explicit wait_seconds=60 and injected monotonic/sleep and assert the deadline honors 60: at least one sleep recorded before timeout), and test_gate_functions_source_defaults_from_settings (same env-and-singleton-reset arrangement with wait 0: required_checks_green and evaluate_merge_gate called WITHOUT wait/poll arguments fail fast with zero sleeps; restore the singleton to None afterwards so later tests rebuild it).
- **Commit**: `feat(auto_merge): settings-sourced CI-gate wait/poll with fail-fast zero`
- **Done when**: pytest tests/unit/test_automerge_fail_fast_gate.py tests/unit/test_review_auto_merge.py tests/unit/test_merge_gate.py tests/unit/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/review/auto_merge.py tests/unit/test_automerge_fail_fast_gate.py exits 0
- **Unit tests**: `tests/unit/test_automerge_fail_fast_gate.py::test_wait_zero_single_evaluation_no_sleep`, `tests/unit/test_automerge_fail_fast_gate.py::test_run_auto_merge_wait_zero_persists_fail_fast_skip`, `tests/unit/test_automerge_fail_fast_gate.py::test_explicit_wait_argument_beats_settings`, `tests/unit/test_automerge_fail_fast_gate.py::test_gate_functions_source_defaults_from_settings`

## Step 3 — end-to-end integration: env-driven fail-fast skip persists and never merges

- **Files**: `tests/integration/test_automerge_fail_fast_gate.py`
- **Action**: Create tests/integration/test_automerge_fail_fast_gate.py with one test test_automerge_fail_fast_settings_end_to_end that exercises the WHOLE settings-to-outcome path with no explicit wait arguments: set FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 in the environment, reset the cached settings singleton (assign ferova.core.config._settings = None, restore to None in a finally block), build a truthful boundary-fake GhCli whose statusCheckRollup keeps one required check QUEUED (reuse the fixture pattern of tests/unit/test_review_auto_merge.py including the ls-remote convergence answers), call run_auto_merge(pr_number, gh=fake, db_path=tmp sqlite path) WITHOUT wait_seconds/poll_interval, and assert: the returned outcome is SKIP_CI_TIMEOUT, a pr_merges row with that outcome was persisted, the squash-merge endpoint was never invoked, and an injected sleep recorder was never called.
- **Commit**: `test(integration): env-driven fail-fast automerge skip end to end`
- **Done when**: pytest tests/integration/test_automerge_fail_fast_gate.py -x -q passes; ruff check tests/integration/test_automerge_fail_fast_gate.py exits 0
- **Unit tests**: `tests/integration/test_automerge_fail_fast_gate.py::test_automerge_fail_fast_settings_end_to_end`

## Integration tests

- `tests/integration/test_automerge_fail_fast_gate.py::test_automerge_fail_fast_settings_end_to_end`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-AUTOMERGE-EVENT-DRIVEN",
  "title": "Lane 1: settings-sourced CI-gate wait with a fail-fast zero contract",
  "summary": "Add FEROVA_AUTOMERGE_CI_* settings, source the four gate functions' wait/poll defaults from them at call time via a None sentinel, retire the module constants, define wait=0 as exactly one rollup evaluation with zero sleeps, and prove the whole path end to end. Lane 2 (workflows, .env.example) is operator-manual and out of scope.",
  "steps": [
    {
      "index": 1,
      "title": "Settings gain the two automerge CI-gate knobs",
      "files": [
        "src/ferova/core/config.py",
        "tests/unit/test_automerge_fail_fast_gate.py"
      ],
      "action": "In the Settings class in config.py, add two fields following the module's existing Field pattern (env_prefix supplies the FEROVA_ name): automerge_ci_wait_seconds: int = Field(default=720, ge=0, description=\"Total CI-gate wait budget in seconds; 0 = single evaluation, fail fast.\") and automerge_ci_poll_interval: int = Field(default=30, ge=1, description=\"Seconds between CI rollup polls when the budget allows waiting.\"). Create tests/unit/test_automerge_fail_fast_gate.py with one test test_settings_env_overrides_wait_and_poll that: builds Settings(_env_file=None) with FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 and FEROVA_AUTOMERGE_CI_POLL_INTERVAL=5 set in the environment and asserts the fields are 0 and 5; builds Settings(_env_file=None) with both vars absent and asserts 720 and 30; asserts pydantic.ValidationError is raised for FEROVA_AUTOMERGE_CI_WAIT_SECONDS=-1 and for FEROVA_AUTOMERGE_CI_POLL_INTERVAL=0. Always pass _env_file=None so the test is immune to env-file anchoring changes.",
      "commit_message": "feat(config): automerge CI-gate wait/poll settings",
      "done_when": "pytest tests/unit/test_automerge_fail_fast_gate.py tests/unit/test_settings_chains_env_precedence.py tests/unit/test_settings_sharp_prefix_aliases.py -x -q passes; ruff check src/ferova/core/config.py tests/unit/test_automerge_fail_fast_gate.py exits 0",
      "unit_tests": [
        "tests/unit/test_automerge_fail_fast_gate.py::test_settings_env_overrides_wait_and_poll"
      ]
    },
    {
      "index": 2,
      "title": "auto_merge sources wait/poll defaults from settings; fail-fast zero contract",
      "files": [
        "src/ferova/review/auto_merge.py",
        "tests/unit/test_automerge_fail_fast_gate.py"
      ],
      "action": "In auto_merge.py change the wait_seconds and poll_interval parameters of evaluate_ci_gate, required_checks_green, evaluate_merge_gate and run_auto_merge from int defaults to int | None = None, and resolve them at call time through a new private helper _resolve_wait_poll(wait_seconds: int | None, poll_interval: int | None) -> tuple[int, int] that returns the argument when not None and otherwise the value from ferova.core.config.get_settings() (automerge_ci_wait_seconds / automerge_ci_poll_interval). Delete the module constants _DEFAULT_WAIT_SECONDS and _DEFAULT_POLL_INTERVAL. decide_at_head is deliberately untouched (it consumes an already-computed ci_green). Update the docstrings that describe the retired 12-minute window: the module docstring bullet about _DEFAULT_WAIT_SECONDS matching the auto-review.yml wait, and the evaluate_ci_gate docstring line citing SP-AUTOMERGE-CI-GATE, both now state that defaults come from Settings (FEROVA_AUTOMERGE_CI_WAIT_SECONDS / FEROVA_AUTOMERGE_CI_POLL_INTERVAL) and that wait_seconds=0 means exactly one rollup evaluation with zero sleeps (SP-AUTOMERGE-EVENT-DRIVEN). Append to tests/unit/test_automerge_fail_fast_gate.py four tests: test_wait_zero_single_evaluation_no_sleep (evaluate_ci_gate with wait_seconds=0 against a truthful boundary-fake GhCli whose rollup keeps one required check QUEUED, an injected sleep recorder and injected monotonic: exactly one rollup read, recorder never called, outcome SKIP_CI_TIMEOUT, reason names the pending check), test_run_auto_merge_wait_zero_persists_fail_fast_skip (run_auto_merge with wait_seconds=0, fake GhCli with a pending required check, real sqlite db_path in tmp_path: persists a pr_merges row with outcome SKIP_CI_TIMEOUT and the merge endpoint is never called — reuse the fixture pattern of tests/unit/test_review_auto_merge.py), test_explicit_wait_argument_beats_settings (set FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 in the environment and reset the cached settings singleton by assigning ferova.core.config._settings = None, then call evaluate_ci_gate with explicit wait_seconds=60 and injected monotonic/sleep and assert the deadline honors 60: at least one sleep recorded before timeout), and test_gate_functions_source_defaults_from_settings (same env-and-singleton-reset arrangement with wait 0: required_checks_green and evaluate_merge_gate called WITHOUT wait/poll arguments fail fast with zero sleeps; restore the singleton to None afterwards so later tests rebuild it).",
      "commit_message": "feat(auto_merge): settings-sourced CI-gate wait/poll with fail-fast zero",
      "done_when": "pytest tests/unit/test_automerge_fail_fast_gate.py tests/unit/test_review_auto_merge.py tests/unit/test_merge_gate.py tests/unit/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/review/auto_merge.py tests/unit/test_automerge_fail_fast_gate.py exits 0",
      "unit_tests": [
        "tests/unit/test_automerge_fail_fast_gate.py::test_wait_zero_single_evaluation_no_sleep",
        "tests/unit/test_automerge_fail_fast_gate.py::test_run_auto_merge_wait_zero_persists_fail_fast_skip",
        "tests/unit/test_automerge_fail_fast_gate.py::test_explicit_wait_argument_beats_settings",
        "tests/unit/test_automerge_fail_fast_gate.py::test_gate_functions_source_defaults_from_settings"
      ]
    },
    {
      "index": 3,
      "title": "end-to-end integration: env-driven fail-fast skip persists and never merges",
      "files": [
        "tests/integration/test_automerge_fail_fast_gate.py"
      ],
      "action": "Create tests/integration/test_automerge_fail_fast_gate.py with one test test_automerge_fail_fast_settings_end_to_end that exercises the WHOLE settings-to-outcome path with no explicit wait arguments: set FEROVA_AUTOMERGE_CI_WAIT_SECONDS=0 in the environment, reset the cached settings singleton (assign ferova.core.config._settings = None, restore to None in a finally block), build a truthful boundary-fake GhCli whose statusCheckRollup keeps one required check QUEUED (reuse the fixture pattern of tests/unit/test_review_auto_merge.py including the ls-remote convergence answers), call run_auto_merge(pr_number, gh=fake, db_path=tmp sqlite path) WITHOUT wait_seconds/poll_interval, and assert: the returned outcome is SKIP_CI_TIMEOUT, a pr_merges row with that outcome was persisted, the squash-merge endpoint was never invoked, and an injected sleep recorder was never called.",
      "commit_message": "test(integration): env-driven fail-fast automerge skip end to end",
      "done_when": "pytest tests/integration/test_automerge_fail_fast_gate.py -x -q passes; ruff check tests/integration/test_automerge_fail_fast_gate.py exits 0",
      "unit_tests": [
        "tests/integration/test_automerge_fail_fast_gate.py::test_automerge_fail_fast_settings_end_to_end"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_automerge_fail_fast_gate.py::test_automerge_fail_fast_settings_end_to_end"
  ]
}
```
