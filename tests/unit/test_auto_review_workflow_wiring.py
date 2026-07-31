"""Guard tests locking the auto_fix job's SP-VERIFIER-PR-TREE + SP-AUTOFIX-LEDGER-HYDRATE wiring.

`.github/workflows/*` is bot-forbidden and has no runtime test, so these
parse the committed workflow and assert the two hand-applied fixes stay in
place: the writable checkout resolves the PR head branch via ``gh`` (not the
trigger-shape-specific ``pull_request.head.ref``), and the Coder's findings
ledger is hydrated from the review artifact before the fix runs.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "auto-review.yml"


def _auto_fix_steps() -> list[dict]:
    workflow = yaml.safe_load(_WORKFLOW.read_text(encoding="utf-8"))
    return workflow["jobs"]["auto_fix"]["steps"]


def _step_named(steps: list[dict], name: str) -> dict:
    for step in steps:
        if step.get("name") == name:
            return step
    raise AssertionError(f"auto_fix job has no step named {name!r}")


def test_writable_checkout_ref_resolves_via_gh_not_trigger_shape() -> None:
    steps = _auto_fix_steps()
    resolve = _step_named(steps, "Resolve PR head branch")
    assert "gh pr view" in resolve["run"]
    assert "headRefName" in resolve["run"]
    checkout = _step_named(steps, "Checkout PR head (writable)")
    assert checkout["with"]["ref"] == "${{ steps.pr_branch.outputs.ref }}"
    assert "pull_request.head.ref" not in checkout["with"]["ref"]


def test_resolve_branch_precedes_writable_checkout() -> None:
    names = [s.get("name") for s in _auto_fix_steps()]
    assert names.index("Resolve PR head branch") < names.index("Checkout PR head (writable)")


def test_findings_ledger_downloaded_before_coder_fix() -> None:
    steps = _auto_fix_steps()
    download = _step_named(steps, "Download findings ledger")
    assert download.get("continue-on-error") is True
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"]["name"] == "findings-ledger-${{ needs.review.outputs.pr_number }}"
    assert download["with"]["path"] == "${{ runner.temp }}"
    names = [s.get("name") for s in steps]
    assert names.index("Download findings ledger") < names.index("Run Coder auto-fix")
