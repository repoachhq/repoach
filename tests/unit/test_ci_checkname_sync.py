"""Guards that :data:`DEFAULT_REQUIRED_CHECK_NAMES` derives from ci.yml's matrix.

`src/repoach/review/auto_merge.py`'s ``DEFAULT_REQUIRED_CHECK_NAMES`` is a
hardcoded tuple hand-maintained to mirror `.github/workflows/ci.yml`'s
``jobs.test.strategy.matrix.python-version`` combined with the job's
``name: Test suite (Python ${{ matrix.python-version }})`` template.
Nothing previously enforced that the two stay in sync: a future matrix
edit that forgets to update the constant would silently desynchronize
the auto-merge CI gate from the real required-check names GitHub reports.
This module parses the real workflow file and independently derives the
expected check-name set, then asserts it against the constant, so a
matrix edit without a matching constant update fails CI immediately.
"""

from pathlib import Path

import yaml

from repoach.review.auto_merge import DEFAULT_REQUIRED_CHECK_NAMES

_CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_MATRIX_PLACEHOLDER = "${{ matrix.python-version }}"


def _derive_expected_check_names(python_versions: list[str], name_template: str) -> set[str]:
    return {name_template.replace(_MATRIX_PLACEHOLDER, version) for version in python_versions}


def test_default_required_check_names_matches_ci_matrix() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())
    test_job = workflow["jobs"]["test"]
    python_versions = test_job["strategy"]["matrix"]["python-version"]
    name_template = test_job["name"]

    derived_check_names = _derive_expected_check_names(python_versions, name_template)

    assert derived_check_names == set(DEFAULT_REQUIRED_CHECK_NAMES)


def test_matrix_desync_is_detected() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())
    name_template = workflow["jobs"]["test"]["name"]

    bumped_python_versions = ["3.11", "3.12", "3.13"]
    derived_check_names = _derive_expected_check_names(bumped_python_versions, name_template)

    assert derived_check_names != set(DEFAULT_REQUIRED_CHECK_NAMES)
