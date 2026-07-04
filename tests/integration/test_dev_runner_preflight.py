"""SP-DEV-STEP-PREFLIGHT integration — end-to-end preflight skip path.

Exercises the full preflight skip against a real git repo and a real
pytest invocation: files pre-seeded on a branch with a commit message
that does NOT match the step's own message, then the session loop
detects the step as preflight-complete, commits any uncommitted work,
and never dispatches the Developer.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from ferova.review.dev_runner import run_developer_session
from ferova.review.persistence import init_schema
from ferova.review.plan import (
    ActionPlan,
    PlanStep,
    plan_relpath,
    render_plan_markdown,
)

_SPEC_ID = "SP-PREFLIGHT-INT"

_MARKER_MODULE = '"""Preflight marker module — exists so the preflight predicate sees the file."""\n\nMARKER = "preflight"\n'
_MARKER_TEST = '"""Preflight marker test — hermetic: reads the sibling file, imports nothing.\n\nImporting the marker as a package module would resolve against the\ninstalled ferova (the editable install), not this seeded repo; and a\nstep file under src/ would trip the plan-form interlock requiring an\nintegration test promise. Both traps killed the first version.\n"""\n\nfrom pathlib import Path\n\n\ndef test_marker() -> None:\n    """Assert the marker module sits beside this test."""\n    marker = Path(__file__).with_name("preflight_marker.py")\n    assert \'MARKER = "preflight"\' in marker.read_text(encoding="utf-8")\n'


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _one_step_preflight_plan() -> ActionPlan:
    """Return a plan whose single step covers the pre-seeded marker files."""
    return ActionPlan(
        spec_id=_SPEC_ID,
        title="Preflight integration demo",
        summary="Step already done — preflight should skip the Developer.",
        steps=[
            PlanStep(
                index=1,
                title="Add preflight marker module",
                files=[
                    "tests/unit/preflight_marker.py",
                    "tests/unit/test_preflight_marker.py",
                ],
                action="Create the marker module and its test.",
                commit_message="feat(preflight): add marker module",
                done_when="pytest tests/unit/test_preflight_marker.py is green",
                unit_tests=["tests/unit/test_preflight_marker.py"],
            ),
        ],
        integration_tests=[],
    )


def _recording_developer() -> MagicMock:
    """Fake Developer that records every call but never writes anything.

    The preflight path should never invoke it — any call is a test failure.
    """
    dev = MagicMock()

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        return MagicMock(text="should not be called", model_used="fake", turns=0, tokens_used=0)

    dev.develop_step.side_effect = _step
    return dev


def _compliant_judge(prompt: str) -> str:
    return '{"compliant": true, "reasons": "ok", "gaps": []}'


def test_preflight_skip_path_end_to_end(tmp_path: Path, monkeypatch) -> None:
    """Full preflight skip: files pre-seeded, commit message mismatched, Developer never called.

    Steps:
    1. Init a git repo with ``develop`` as the base branch.
    2. Create a feature branch off ``develop``.
    3. Write the marker module and its passing test.
    4. Commit them with a message that does NOT match the step's commit_message.
    5. Seed the plan document on the feature branch.
    6. Run ``run_developer_session`` with a recording Developer fake.
    7. Assert steps_completed == 1, Developer never called, and a preflight
       audit row exists.
    """
    repo = tmp_path / "repo"
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "integration").mkdir(parents=True)
    (repo / "docs" / "specs").mkdir(parents=True)
    (repo / "docs" / "specs" / f"2026-07-04_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — preflight demo\n\n## Why\n\nIntegration test.\n\n## Definition of Done\n\n- works\n",
        encoding="utf-8",
    )
    (repo / "README.md").write_text("preflight demo\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )

    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")

    branch = "feat/sp-preflight-int-impl"
    _git(repo, "switch", "-c", branch, "develop")

    (repo / "tests" / "unit" / "preflight_marker.py").write_text(_MARKER_MODULE, encoding="utf-8")
    (repo / "tests" / "unit" / "test_preflight_marker.py").write_text(
        _MARKER_TEST, encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: pre-seed marker (absorbed into earlier commit)")

    plan = _one_step_preflight_plan()
    target = repo / plan_relpath(_SPEC_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")

    db_path = tmp_path / "test.db"
    init_schema(db_path)

    monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)

    dev = _recording_developer()

    result = run_developer_session(
        _SPEC_ID,
        repo_root=repo,
        branch=branch,
        developer=dev,
        push=False,
        db_path=db_path,
        judge=_compliant_judge,
    )

    assert result.steps_completed == 1
    dev.develop_step.assert_not_called()

    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{db_path}")
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT model_used, tokens_used FROM pr_coder_responses")
        ).fetchall()
    assert any(row[0] == "preflight" and row[1] == 0 for row in rows), (
        f"expected a preflight audit row, got: {rows}"
    )
