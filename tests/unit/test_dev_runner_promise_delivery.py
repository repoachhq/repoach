"""SP-PROMISE-RENAME-RETIRE — the promise gate resists mechanical laundering.

The pre-fix step gate mechanically renamed a lone unrelated green test
into the promised selector's name and accepted the step, regardless of
what the renamed test actually asserted. This is the discriminating
regression test: a step whose promised selector is absent, where the
loop delivers exactly one unrelated but green ``def test_*`` into the
promised file, must FAIL the promise gate — never rename its way to a
false accept.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from repoach.review.dev_runner import execute_plan_step
from repoach.review.devagent_loop import DevLoopResult
from repoach.review.persistence import init_schema
from repoach.review.plan import ActionPlan, PlanStep


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


def _init_repo(repo: Path) -> None:
    (repo / "src").mkdir(parents=True)
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    (repo / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.invalid")
    _git(repo, "config", "user.name", "T")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")


def test_unrelated_green_test_fails_promise_not_renamed(tmp_path: Path) -> None:
    """A single unrelated-but-green delivered test never launders the promise.

    The loop (a truthful boundary fake) writes exactly one green
    ``def test_unrelated_delivery(`` into the promised test file on
    every attempt — never the promised ``test_promised_delivery``. The
    step must exhaust its retries and FAIL the promise gate, and the
    promised file on disk must still lack the promised definition —
    confirming no mechanical rename ever laundered the promise.
    """
    repo = tmp_path / "repo"
    _init_repo(repo)

    step = PlanStep(
        index=1,
        title="Add the promised test",
        files=["tests/unit/test_laundering.py"],
        action="Write the test file.",
        commit_message="test(laundering): add promised test",
        done_when="pytest tests/unit/test_laundering.py exits 0",
        unit_tests=["tests/unit/test_laundering.py::test_promised_delivery"],
    )
    plan = ActionPlan(
        spec_id="SP-PROMISE-RENAME-RETIRE-TEST",
        title="No mechanical promise laundering",
        summary="One step whose loop only ever delivers an unrelated green test.",
        steps=[step],
        integration_tests=[],
    )

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        target = Path(repo_root) / "tests" / "unit" / "test_laundering.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("def test_unrelated_delivery():\n    assert True\n", encoding="utf-8")
        return DevLoopResult(text="done", model_used="fake/model", turns=1, tokens_used=1)

    developer = MagicMock()
    developer.develop_step.side_effect = _step
    db = tmp_path / "t.db"
    init_schema(db)

    outcome = execute_plan_step(
        step, plan=plan, repo_root=repo, developer=developer, repo_tree="", db=db
    )

    assert outcome.ok is False
    on_disk = (repo / "tests" / "unit" / "test_laundering.py").read_text(encoding="utf-8")
    assert "def test_promised_delivery(" not in on_disk
    assert "def test_unrelated_delivery(" in on_disk
