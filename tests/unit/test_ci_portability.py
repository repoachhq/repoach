"""Guard tests locking the fork-portable CI wiring.

`.github/workflows/*` is bot-forbidden and hand-maintained. These parse every
committed workflow and assert the runner and the actor allowlist stay sourced
from repository variables (`REPOACH_RUNNER`, `REPOACH_ALLOWED_ACTORS`) with the
maintainer defaults as fallbacks, so a fork configures both in one place each
instead of editing hardcoded literals across four files.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

_WORKFLOW_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
_RUNNER_EXPR = "${{ vars.REPOACH_RUNNER || 'self-hosted' }}"
_REVIEW_RUNNER_EXPR = "${{ vars.REPOACH_REVIEW_RUNNER || vars.REPOACH_RUNNER || 'self-hosted' }}"
_REVIEW_RUNNER_JOBS = {
    ("auto-review.yml", "review"),
    ("auto-review.yml", "auto_merge"),
}
"""Jobs that prefer a dedicated review runner when one is configured.

``review`` and ``auto_merge`` execute no PR-authored code (unlike ``auto_fix``), so
they may leave the CI runners free; unset, they fall back to ``REPOACH_RUNNER``.
"""
_GITHUB_HOSTED = {
    ("auto-review.yml", "coalesce"),
    ("auto-review.yml", "rearm"),
    ("auto-review.yml", "retry"),
    ("publish.yml", "build-and-publish"),
}
"""Jobs that run GitHub-hosted on purpose (SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF D1).

``coalesce`` and ``rearm`` only sleep, and ``retry`` waits 300 s before it
dispatches (SP-REVIEW-SURVIVES-TRANSPORT-FAILURES): none may queue behind
the self-hosted runner, and on the public repository they cost no minutes.
``build-and-publish`` runs ``pypa/gh-action-pypi-publish``, a Docker action:
the self-hosted runner has no Docker (publish run 44044713038 failed with
``docker: command not found``), so the upload job alone is GitHub-hosted.
"""


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOW_DIR.glob("*.yml"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_runs_on_is_the_configurable_runner(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job_id, job in workflow["jobs"].items():
        if (path.name, job_id) in _GITHUB_HOSTED:
            assert job["runs-on"] == "ubuntu-latest"
            continue
        if (path.name, job_id) in _REVIEW_RUNNER_JOBS:
            assert job["runs-on"] == _REVIEW_RUNNER_EXPR
            continue
        assert job["runs-on"] == _RUNNER_EXPR


def test_auto_fix_stays_on_the_ci_runner() -> None:
    workflow = yaml.safe_load((_WORKFLOW_DIR / "auto-review.yml").read_text(encoding="utf-8"))
    assert workflow["jobs"]["auto_fix"]["runs-on"] == _RUNNER_EXPR


def test_no_hardcoded_self_hosted_or_actor_literal_remains() -> None:
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        assert "runs-on: self-hosted" not in text
        assert "fromJSON('[\"jwfaye\"]')" not in text


def test_actor_gates_source_the_allowlist_variable() -> None:
    gated = 0
    for path in _workflow_files():
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            if "github.actor" in line and "fromJSON" in line:
                assert "vars.REPOACH_ALLOWED_ACTORS" in line
                gated += 1
    assert gated >= 1
