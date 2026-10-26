"""Guards that :data:`DEFAULT_REQUIRED_CHECK_NAMES` derives from every job of ci.yml.

The default list of required checks is hand-maintained to mirror
`.github/workflows/ci.yml`. SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF D4
widened it from the two test suites to every job of the ``CI`` workflow,
matrix expanded: both test suites, the wheel build and both wheel smokes
(a wheel that does not install is a red the merge must see). This module
parses the real workflow file, derives the expected names independently
(each job's ``name`` template with every matrix value substituted), and
asserts them against the constant, so a job or matrix edit without a
matching constant update fails CI immediately.
"""

from pathlib import Path

import yaml

from repoach.review.auto_merge import DEFAULT_REQUIRED_CHECK_NAMES

_CI_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "ci.yml"
_MATRIX_PLACEHOLDER = "${{ matrix.python-version }}"


def _derive_expected_check_names(workflow: dict) -> set[str]:
    names: set[str] = set()
    for job_id, job in workflow["jobs"].items():
        template = job.get("name", job_id)
        versions = job.get("strategy", {}).get("matrix", {}).get("python-version")
        if versions:
            names |= {template.replace(_MATRIX_PLACEHOLDER, str(v)) for v in versions}
        else:
            names.add(template)
    return names


def test_default_required_check_names_matches_ci_matrix() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())

    assert _derive_expected_check_names(workflow) == set(DEFAULT_REQUIRED_CHECK_NAMES)
    assert len(DEFAULT_REQUIRED_CHECK_NAMES) == 5


def test_matrix_desync_is_detected() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())
    workflow["jobs"]["test"]["strategy"]["matrix"]["python-version"] = ["3.11", "3.12", "3.13"]

    assert _derive_expected_check_names(workflow) != set(DEFAULT_REQUIRED_CHECK_NAMES)


def test_dropped_job_is_detected() -> None:
    workflow = yaml.safe_load(_CI_WORKFLOW_PATH.read_text())
    del workflow["jobs"]["wheel-build"]

    assert _derive_expected_check_names(workflow) != set(DEFAULT_REQUIRED_CHECK_NAMES)
