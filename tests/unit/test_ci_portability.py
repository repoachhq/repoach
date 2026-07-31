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


def _workflow_files() -> list[Path]:
    return sorted(_WORKFLOW_DIR.glob("*.yml"))


@pytest.mark.parametrize("path", _workflow_files(), ids=lambda p: p.name)
def test_every_runs_on_is_the_configurable_runner(path: Path) -> None:
    workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
    for job in workflow["jobs"].values():
        assert job["runs-on"] == _RUNNER_EXPR


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
