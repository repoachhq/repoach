# SP-INTEGRATION-STAGE — Integration tests CI stage

Add integration test stage to ci_local.sh with --integration flag support, unit tests to verify the contract, and documentation update.

## Step 1 — Add integration stage in ci_local.sh with --integration flag and contract tests

- **Files**: `scripts/ci_local.sh`, `tests/unit/test_ci_local_integration_stage.py`
- **Action**: Modify scripts/ci_local.sh to add integration test stage that runs after unit tests, add --integration flag handling, and ensure it only runs in default mode. Create tests/unit/test_ci_local_integration_stage.py with tests that verify the script contains 'tests/integration', the --integration flag is declared, and --fast does not invoke integration stage.
- **Commit**: `feat(ci): integration stage in ci_local.sh with --integration flag and contract tests`
- **Done when**: bash -n scripts/ci_local.sh && pytest tests/unit/test_ci_local_integration_stage.py passes
- **Unit tests**: `tests/unit/test_ci_local_integration_stage.py::test_script_contains_tests_integration_string`, `tests/unit/test_ci_local_integration_stage.py::test_integration_flag_declared`, `tests/unit/test_ci_local_integration_stage.py::test_fast_mode_does_not_invoke_integration`

## Step 2 — Document --integration flag in CLAUDE.md

- **Files**: `CLAUDE.md`, `tests/unit/test_claude_md_integration_flag.py`
- **Action**: Update CLAUDE.md to document the new --integration flag in the Local CI mirror section, and create tests/unit/test_claude_md_integration_flag.py to verify the documentation is properly updated.
- **Commit**: `docs: document --integration in CLAUDE.md`
- **Done when**: grep -q 'scripts/ci_local.sh --integration' CLAUDE.md && pytest tests/unit/test_claude_md_integration_flag.py passes
- **Unit tests**: `tests/unit/test_claude_md_integration_flag.py`

## Integration tests

- `tests/integration/test_developer_session.py`

<!-- ferova-action-plan -->
```json
{
  "spec_id": "SP-INTEGRATION-STAGE",
  "title": "Integration tests CI stage",
  "summary": "Add integration test stage to ci_local.sh with --integration flag support, unit tests to verify the contract, and documentation update.",
  "steps": [
    {
      "index": 1,
      "title": "Add integration stage in ci_local.sh with --integration flag and contract tests",
      "files": [
        "scripts/ci_local.sh",
        "tests/unit/test_ci_local_integration_stage.py"
      ],
      "action": "Modify scripts/ci_local.sh to add integration test stage that runs after unit tests, add --integration flag handling, and ensure it only runs in default mode. Create tests/unit/test_ci_local_integration_stage.py with tests that verify the script contains 'tests/integration', the --integration flag is declared, and --fast does not invoke integration stage.",
      "commit_message": "feat(ci): integration stage in ci_local.sh with --integration flag and contract tests",
      "done_when": "bash -n scripts/ci_local.sh && pytest tests/unit/test_ci_local_integration_stage.py passes",
      "unit_tests": [
        "tests/unit/test_ci_local_integration_stage.py::test_script_contains_tests_integration_string",
        "tests/unit/test_ci_local_integration_stage.py::test_integration_flag_declared",
        "tests/unit/test_ci_local_integration_stage.py::test_fast_mode_does_not_invoke_integration"
      ]
    },
    {
      "index": 2,
      "title": "Document --integration flag in CLAUDE.md",
      "files": [
        "CLAUDE.md",
        "tests/unit/test_claude_md_integration_flag.py"
      ],
      "action": "Update CLAUDE.md to document the new --integration flag in the Local CI mirror section, and create tests/unit/test_claude_md_integration_flag.py to verify the documentation is properly updated.",
      "commit_message": "docs: document --integration in CLAUDE.md",
      "done_when": "grep -q 'scripts/ci_local.sh --integration' CLAUDE.md && pytest tests/unit/test_claude_md_integration_flag.py passes",
      "unit_tests": [
        "tests/unit/test_claude_md_integration_flag.py"
      ]
    }
  ],
  "integration_tests": [
    "tests/integration/test_developer_session.py"
  ]
}
```
