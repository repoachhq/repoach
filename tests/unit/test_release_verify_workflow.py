"""Guard test locking the SP-RELEASE-SANCTIONED-DEVELOP-MERGE G3 push workflow.

`.github/workflows/*` is bot-forbidden and hand-maintained, so this parses the
committed workflow and asserts it stays a push-to-main trigger that runs the
receipt-free `repoach release verify --live` check.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release-verify.yml"


def _workflow() -> dict:
    return yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))


def test_triggers_on_push_to_main() -> None:
    workflow = _workflow()
    on = workflow[True] if True in workflow else workflow["on"]
    assert on["push"]["branches"] == ["main"]


def test_runs_release_verify_live() -> None:
    steps = _workflow()["jobs"]["verify"]["steps"]
    run_bodies = "\n".join(s.get("run", "") for s in steps)
    assert "repoach release verify --live" in run_bodies
