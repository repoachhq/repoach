# SP-MERGE-EXIT-CONTRACT — review merge exit contract — every non-fatal skip exits 5

Move the exit-code classification next to the outcome vocabulary in auto_merge.py, add a total merge_exit_code helper, wire the CLI to it, and guard exhaustiveness with a reflection test so a future outcome constant can never silently regress to exit 1.

## Step 1 — Add outcome classification sets and merge_exit_code helper

- **Files**: `src/ferova/review/auto_merge.py`, `tests/unit/test_review_merge_exit_contract.py`
- **Action**: In auto_merge.py, immediately after the OUTCOME_* constants, add two module-level frozensets: SUCCESS_OUTCOMES = frozenset({OUTCOME_MERGED, OUTCOME_ALREADY_MERGED}) and NON_FATAL_SKIP_OUTCOMES = frozenset({OUTCOME_SKIP_BASE, OUTCOME_SKIP_GATE, OUTCOME_SKIP_CI, OUTCOME_SKIP_CI_FAILED, OUTCOME_SKIP_CI_TIMEOUT, OUTCOME_SKIP_CI_MISSING, OUTCOME_SKIP_STALE_HEAD}). Add a pure function merge_exit_code(outcome: str) -> int that returns 0 for SUCCESS_OUTCOMES, 5 for NON_FATAL_SKIP_OUTCOMES, and 1 for OUTCOME_FAILED or any unrecognised string. Replace the inline set literal {OUTCOME_MERGED, OUTCOME_ALREADY_MERGED} at line 757 with SUCCESS_OUTCOMES. Create tests/unit/test_review_merge_exit_contract.py with: test_every_outcome_constant_is_classified (inspect.getmembers over ferova.review.auto_merge, filter names starting with OUTCOME_, assert every value is in SUCCESS_OUTCOMES, NON_FATAL_SKIP_OUTCOMES, or equals OUTCOME_FAILED), test_merge_exit_code_success_outcomes_zero (parametrized over both success outcomes), test_merge_exit_code_non_fatal_skips_five (parametrized over all seven skip outcomes including SKIP_CI_TIMEOUT and SKIP_STALE_HEAD), and test_merge_exit_code_failed_and_unknown_one (FAILED and an arbitrary string both map to 1). Do NOT touch src/ferova/cli/review_cmds.py in this step: the CLI still carries the old if-chain mapping, and any test asserting CLI exit codes against merge_exit_code would fail until step 2 wires it.
- **Commit**: `feat(auto_merge): add outcome classification sets and merge_exit_code helper`
- **Done when**: pytest tests/unit/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/review/auto_merge.py tests/unit/test_review_merge_exit_contract.py exits 0
- **Unit tests**: `tests/unit/test_review_merge_exit_contract.py::test_every_outcome_constant_is_classified`, `tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_success_outcomes_zero`, `tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_non_fatal_skips_five`, `tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_failed_and_unknown_one`

## Step 2 — Wire review_merge CLI to merge_exit_code and add integration tests

- **Files**: `src/ferova/cli/review_cmds.py`, `tests/unit/test_review_merge_exit_contract.py`, `tests/integration/test_review_merge_exit_contract.py`
- **Action**: In review_cmds.py, extend the import from ..review.auto_merge to also import merge_exit_code. In the review_merge command body, remove the hand-written if-chain that maps outcomes to typer.Exit and replace it with a single raise typer.Exit(code=merge_exit_code(result.outcome)) placed after the JSON echo. Update the command docstring to enumerate the outcomes behind each exit code (0: APPROVE, ALREADY_MERGED; 5: SKIP_BASE, SKIP_GATE, SKIP_CI_RED, SKIP_CI_FAILED, SKIP_CI_TIMEOUT, SKIP_CI_MISSING, SKIP_STALE_HEAD; 1: FAILED and anything unrecognised). Append to tests/unit/test_review_merge_exit_contract.py a test test_cli_review_merge_exit_code_mapping that replaces run_auto_merge at the CLI seam with a callable returning a genuine AutoMergeResult (the pattern from tests/unit/test_release_cli.py), invokes the merge command on review_cmds.review_app through typer.testing.CliRunner (the tests/unit/test_review_lessons.py invocation pattern), and asserts result.exit_code == merge_exit_code(outcome) for a granular skip outcome (SKIP_CI_TIMEOUT, expecting 5) plus that the JSON output is still printed. Create tests/integration/test_review_merge_exit_contract.py with test_cli_review_merge_skip_ci_timeout_exits_five and test_cli_review_merge_failed_exits_one, each replacing run_auto_merge at the CLI seam with a callable returning a genuine AutoMergeResult, invoking the merge command via CliRunner, and asserting result.exit_code == 5 (for SKIP_CI_TIMEOUT) and result.exit_code == 1 (for FAILED) respectively, plus that the JSON output is still printed.
- **Commit**: `fix(cli): wire review_merge exit code to merge_exit_code helper`
- **Done when**: pytest tests/unit/test_review_merge_exit_contract.py tests/integration/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/cli/review_cmds.py tests/unit/test_review_merge_exit_contract.py tests/integration/test_review_merge_exit_contract.py exits 0
- **Unit tests**: `tests/unit/test_review_merge_exit_contract.py::test_cli_review_merge_exit_code_mapping`

## Integration tests

- `tests/integration/test_review_merge_exit_contract.py::test_cli_review_merge_skip_ci_timeout_exits_five`
- `tests/integration/test_review_merge_exit_contract.py::test_cli_review_merge_failed_exits_one`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-MERGE-EXIT-CONTRACT",
  "title": "review merge exit contract — every non-fatal skip exits 5",
  "summary": "Move the exit-code classification next to the outcome vocabulary in auto_merge.py, add a total merge_exit_code helper, wire the CLI to it, and guard exhaustiveness with a reflection test so a future outcome constant can never silently regress to exit 1.",
  "steps": [
    {
      "index": 1,
      "title": "Add outcome classification sets and merge_exit_code helper",
      "files": [
        "src/ferova/review/auto_merge.py",
        "tests/unit/test_review_merge_exit_contract.py"
      ],
      "action": "In auto_merge.py, immediately after the OUTCOME_* constants, add two module-level frozensets: SUCCESS_OUTCOMES = frozenset({OUTCOME_MERGED, OUTCOME_ALREADY_MERGED}) and NON_FATAL_SKIP_OUTCOMES = frozenset({OUTCOME_SKIP_BASE, OUTCOME_SKIP_GATE, OUTCOME_SKIP_CI, OUTCOME_SKIP_CI_FAILED, OUTCOME_SKIP_CI_TIMEOUT, OUTCOME_SKIP_CI_MISSING, OUTCOME_SKIP_STALE_HEAD}). Add a pure function merge_exit_code(outcome: str) -> int that returns 0 for SUCCESS_OUTCOMES, 5 for NON_FATAL_SKIP_OUTCOMES, and 1 for OUTCOME_FAILED or any unrecognised string. Replace the inline set literal {OUTCOME_MERGED, OUTCOME_ALREADY_MERGED} at line 757 with SUCCESS_OUTCOMES. Create tests/unit/test_review_merge_exit_contract.py with: test_every_outcome_constant_is_classified (inspect.getmembers over ferova.review.auto_merge, filter names starting with OUTCOME_, assert every value is in SUCCESS_OUTCOMES, NON_FATAL_SKIP_OUTCOMES, or equals OUTCOME_FAILED), test_merge_exit_code_success_outcomes_zero (parametrized over both success outcomes), test_merge_exit_code_non_fatal_skips_five (parametrized over all seven skip outcomes including SKIP_CI_TIMEOUT and SKIP_STALE_HEAD), and test_merge_exit_code_failed_and_unknown_one (FAILED and an arbitrary string both map to 1). Do NOT touch src/ferova/cli/review_cmds.py in this step: the CLI still carries the old if-chain mapping, and any test asserting CLI exit codes against merge_exit_code would fail until step 2 wires it.",
      "commit_message": "feat(auto_merge): add outcome classification sets and merge_exit_code helper",
      "done_when": "pytest tests/unit/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/review/auto_merge.py tests/unit/test_review_merge_exit_contract.py exits 0",
      "unit_tests": [
        "tests/unit/test_review_merge_exit_contract.py::test_every_outcome_constant_is_classified",
        "tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_success_outcomes_zero",
        "tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_non_fatal_skips_five",
        "tests/unit/test_review_merge_exit_contract.py::test_merge_exit_code_failed_and_unknown_one"
      ]
    },
    {
      "index": 2,
      "title": "Wire review_merge CLI to merge_exit_code and add integration tests",
      "files": [
        "src/ferova/cli/review_cmds.py",
        "tests/unit/test_review_merge_exit_contract.py",
        "tests/integration/test_review_merge_exit_contract.py"
      ],
      "action": "In review_cmds.py, extend the import from ..review.auto_merge to also import merge_exit_code. In the review_merge command body, remove the hand-written if-chain that maps outcomes to typer.Exit and replace it with a single raise typer.Exit(code=merge_exit_code(result.outcome)) placed after the JSON echo. Update the command docstring to enumerate the outcomes behind each exit code (0: APPROVE, ALREADY_MERGED; 5: SKIP_BASE, SKIP_GATE, SKIP_CI_RED, SKIP_CI_FAILED, SKIP_CI_TIMEOUT, SKIP_CI_MISSING, SKIP_STALE_HEAD; 1: FAILED and anything unrecognised). Append to tests/unit/test_review_merge_exit_contract.py a test test_cli_review_merge_exit_code_mapping that replaces run_auto_merge at the CLI seam with a callable returning a genuine AutoMergeResult (the pattern from tests/unit/test_release_cli.py), invokes the merge command on review_cmds.review_app through typer.testing.CliRunner (the tests/unit/test_review_lessons.py invocation pattern), and asserts result.exit_code == merge_exit_code(outcome) for a granular skip outcome (SKIP_CI_TIMEOUT, expecting 5) plus that the JSON output is still printed. Create tests/integration/test_review_merge_exit_contract.py with test_cli_review_merge_skip_ci_timeout_exits_five and test_cli_review_merge_failed_exits_one, each replacing run_auto_merge at the CLI seam with a callable returning a genuine AutoMergeResult, invoking the merge command via CliRunner, and asserting result.exit_code == 5 (for SKIP_CI_TIMEOUT) and result.exit_code == 1 (for FAILED) respectively, plus that the JSON output is still printed.",
      "commit_message": "fix(cli): wire review_merge exit code to merge_exit_code helper",
      "done_when": "pytest tests/unit/test_review_merge_exit_contract.py tests/integration/test_review_merge_exit_contract.py -x -q passes; ruff check src/ferova/cli/review_cmds.py tests/unit/test_review_merge_exit_contract.py tests/integration/test_review_merge_exit_contract.py exits 0",
      "unit_tests": [
        "tests/unit/test_review_merge_exit_contract.py::test_cli_review_merge_exit_code_mapping"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_review_merge_exit_contract.py::test_cli_review_merge_skip_ci_timeout_exits_five",
    "tests/integration/test_review_merge_exit_contract.py::test_cli_review_merge_failed_exits_one"
  ]
}
```
