# SP-TEST-PARALLEL — Parallelize the test suite with pytest-xdist worksteal

Add pytest-xdist>=3.6 to dev dependencies, pin the serial-neutral addopts contract, update ci_local.sh full-suite invocations with -n auto --dist worksteal, and create a contract test file that verifies all three invariants.

## Step 1 — Add pytest-xdist dev dependency and create contract test file

- **Files**: `pyproject.toml`, `tests/unit/test_ci_parallel_pins.py`
- **Action**: Add "pytest-xdist>=3.6" to the dev list in [project.optional-dependencies].dev (alphabetically after pytest-cov). Create tests/unit/test_ci_parallel_pins.py with two tests using the same textual-contract pattern as test_ci_local_integration_stage.py: (a) test_xdist_is_a_dev_dependency — parse pyproject.toml with tomllib, assert any entry in dev extras starts with "pytest-xdist"; (b) test_addopts_stays_serial_neutral — assert "-n" is absent from tool.pytest.ini_options.addopts. Follow existing file conventions: __future__ annotations import, Path-based reads, Google-style docstrings, no inline comments, test functions return None.
- **Commit**: `build(deps): add pytest-xdist>=3.6 to dev extras and pin parallel contract tests`
- **Done when**: pytest tests/unit/test_ci_parallel_pins.py -v passes
- **Unit tests**: `tests/unit/test_ci_parallel_pins.py::test_xdist_is_a_dev_dependency`, `tests/unit/test_ci_parallel_pins.py::test_addopts_stays_serial_neutral`

## Step 2 — Wire -n auto --dist worksteal into ci_local.sh full-suite invocations

- **Files**: `scripts/ci_local.sh`, `tests/unit/test_ci_parallel_pins.py`
- **Action**: Edit scripts/ci_local.sh: change the full-suite pytest invocations (the run_step "pytest tests/unit" and run_step "pytest tests/integration" lines, currently lines 121 and 127) to append "-n auto --dist worksteal": run_step "pytest tests/unit" python -m pytest -q tests/unit -n auto --dist worksteal and run_step "pytest tests/integration" python -m pytest -q tests/integration -n auto --dist worksteal. Do not touch the lint-only paths, the --fast gate, or the empty-integration-directory skip logic. Then add to tests/unit/test_ci_parallel_pins.py the third contract test test_ci_local_full_suite_runs_parallel — read scripts/ci_local.sh, find every line invoking "pytest" with "tests/unit" or "tests/integration" and assert it carries both "-n auto" and "--dist worksteal". Verify the three test_ci_parallel_pins tests all pass, then execute three consecutive python -m pytest tests/unit -n auto --dist worksteal -q runs and one python -m pytest tests/integration -n auto --dist worksteal -q run to confirm suite stability (AC4). Run ruff check src tests and ruff format --check src tests to confirm zero regressions.
- **Commit**: `perf(ci): enable pytest-xdist worksteal in ci_local.sh full-suite paths`
- **Done when**: pytest tests/unit/test_ci_parallel_pins.py -v passes
- **Unit tests**: `tests/unit/test_ci_parallel_pins.py::test_ci_local_full_suite_runs_parallel`

## Integration tests

_(none promised)_

<!-- repoach-action-plan -->
```json
{
  "spec_id": "SP-TEST-PARALLEL",
  "title": "Parallelize the test suite with pytest-xdist worksteal",
  "summary": "Add pytest-xdist>=3.6 to dev dependencies, pin the serial-neutral addopts contract, update ci_local.sh full-suite invocations with -n auto --dist worksteal, and create a contract test file that verifies all three invariants.",
  "steps": [
    {
      "index": 1,
      "title": "Add pytest-xdist dev dependency and create contract test file",
      "files": [
        "pyproject.toml",
        "tests/unit/test_ci_parallel_pins.py"
      ],
      "action": "Add \"pytest-xdist>=3.6\" to the dev list in [project.optional-dependencies].dev (alphabetically after pytest-cov). Create tests/unit/test_ci_parallel_pins.py with two tests using the same textual-contract pattern as test_ci_local_integration_stage.py: (a) test_xdist_is_a_dev_dependency — parse pyproject.toml with tomllib, assert any entry in dev extras starts with \"pytest-xdist\"; (b) test_addopts_stays_serial_neutral — assert \"-n\" is absent from tool.pytest.ini_options.addopts. Follow existing file conventions: __future__ annotations import, Path-based reads, Google-style docstrings, no inline comments, test functions return None.",
      "commit_message": "build(deps): add pytest-xdist>=3.6 to dev extras and pin parallel contract tests",
      "done_when": "pytest tests/unit/test_ci_parallel_pins.py -v passes",
      "unit_tests": [
        "tests/unit/test_ci_parallel_pins.py::test_xdist_is_a_dev_dependency",
        "tests/unit/test_ci_parallel_pins.py::test_addopts_stays_serial_neutral"
      ]
    },
    {
      "index": 2,
      "title": "Wire -n auto --dist worksteal into ci_local.sh full-suite invocations",
      "files": [
        "scripts/ci_local.sh",
        "tests/unit/test_ci_parallel_pins.py"
      ],
      "action": "Edit scripts/ci_local.sh: change the full-suite pytest invocations (the run_step \"pytest tests/unit\" and run_step \"pytest tests/integration\" lines, currently lines 121 and 127) to append \"-n auto --dist worksteal\": run_step \"pytest tests/unit\" python -m pytest -q tests/unit -n auto --dist worksteal and run_step \"pytest tests/integration\" python -m pytest -q tests/integration -n auto --dist worksteal. Do not touch the lint-only paths, the --fast gate, or the empty-integration-directory skip logic. Then add to tests/unit/test_ci_parallel_pins.py the third contract test test_ci_local_full_suite_runs_parallel — read scripts/ci_local.sh, find every line invoking \"pytest\" with \"tests/unit\" or \"tests/integration\" and assert it carries both \"-n auto\" and \"--dist worksteal\". Verify the three test_ci_parallel_pins tests all pass, then execute three consecutive python -m pytest tests/unit -n auto --dist worksteal -q runs and one python -m pytest tests/integration -n auto --dist worksteal -q run to confirm suite stability (AC4). Run ruff check src tests and ruff format --check src tests to confirm zero regressions.",
      "commit_message": "perf(ci): enable pytest-xdist worksteal in ci_local.sh full-suite paths",
      "done_when": "pytest tests/unit/test_ci_parallel_pins.py -v passes",
      "unit_tests": [
        "tests/unit/test_ci_parallel_pins.py::test_ci_local_full_suite_runs_parallel"
      ]
    }
  ],
  "integration_tests": []
}
```
