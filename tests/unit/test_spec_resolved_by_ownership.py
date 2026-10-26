"""SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS G7 — an undeclared PR is graded by ownership.

A PR that declares no spec (no ``--spec``, no ``Spec:`` line, a branch
that names none) used to be ``not_applicable`` on coverage, so not
declaring a spec escaped it. It is now graded against every governed
spec owning a path of its diff, ownership read at the base; changed
code no spec owns is reported, never refused.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import repoach.core.config as core_config
from repoach.cli.review_cmds import review_app
from repoach.forge import Diff, ForgeWriteBarrier, PullRequest
from repoach.project.spec_source import resolve_spec
from repoach.review.auto_merge import CIGateOutcome, GateEvaluation, decide_at_head
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.spec_gate import coverage_rows_at_head

_SPEC_PATH = "docs/specs/2026-10-26_SP-A_owner.md"
_OWNED = "src/a.py"
_UNOWNED = "src/z.py"
_SELECTOR = "tests/a.py::test_a"
_BRANCH = "fix/tidy-things"
_BODY = "Tidies a few things.\n"


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REPOACH_DB_PATH", "REPOACH_FORGE", "REPOACH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    core_config._settings = None


def _spec(owns: str) -> str:
    return (
        "---\n"
        "id: SP-A\n"
        "title: Owner\n"
        "version: 0.1\n"
        "status: approved\n"
        "owns:\n"
        f"  code: [{owns}]\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n"
        "# Owner\n\n"
        "## Acceptance Criteria\n\n"
        f"- [ ] AC1: `pytest -q {_SELECTOR}` -> green.\n\n"
        "## Open Questions\n\nNone.\n"
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _repo(tmp_path: Path, head: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "docs/specs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / _SPEC_PATH).write_text(_spec(_OWNED), encoding="utf-8")
    (repo / _OWNED).write_text("A = 1\n", encoding="utf-8")
    (repo / _UNOWNED).write_text("Z = 1\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", _BRANCH)
    for path, content in head.items():
        (repo / path).write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, _git(repo, "rev-parse", "HEAD")


def _review(repo: Path, head: str, changed: list[str]) -> TeamOutcome:
    forge = MagicMock()
    forge.diff.return_value = Diff(
        text="".join(f"diff --git a/{p} b/{p}\n" for p in changed), files=changed
    )
    forge.pull_request.return_value = PullRequest(
        number=9, head_ref=_BRANCH, base_ref="develop", body=_BODY
    )
    forge.head_sha.return_value = head
    forge.comments.return_value = []
    forge.barrier = ForgeWriteBarrier()
    orch = ReviewTeamOrchestrator(
        forge=forge,
        db_path=repo.parent / "review.db",
        post_to_github=False,
        repo_root=repo,
        head_sha=head,
    )
    outcome = MagicMock(
        role=MagicMock(value="x"),
        verdict=MagicMock(value="APPROVE"),
        summary="ok",
        comments=[],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="",
    )
    with (
        patch("repoach.review.reviewer.AgentLoop", return_value=MagicMock()),
        patch("repoach.review.reviewer.Architect.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Sentinel.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Tester.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Scribe.review_diff", return_value=outcome),
    ):
        return orch.review_pr(9)


def _decide(repo: Path, head: str) -> tuple[object, object]:
    _, facts, decision = decide_at_head(
        9,
        forge=MagicMock(),
        db=repo.parent / "review.db",
        ci_green=True,
        repo_root=repo,
        head_sha=head,
        max_judged=0,
        head_tree=False,
        base_ref="develop",
    )
    return facts, decision


def test_undeclared_pr_touching_owned_code_is_refused_naming_the_selector(
    tmp_path: Path,
) -> None:
    repo, head = _repo(tmp_path, {_OWNED: "A = 2\n", _UNOWNED: "Z = 2\n"})
    declared = resolve_spec(flag=None, description=_BODY, branch=_BRANCH, root=repo)
    assert declared.spec_id is None

    team = _review(repo, head, [_OWNED, _UNOWNED])
    _, decision = _decide(repo, head)

    assert team.spec_source == "ownership"
    assert team.spec_covered is False
    assert "spec resolved by ownership: SP-A (1 paths)" in team.spec_report
    assert f"ungoverned paths: {_UNOWNED}" in team.spec_report
    assert team.spec_resolution == {
        "source": "ownership",
        "specs": ["SP-A"],
        "ungoverned": [_UNOWNED],
    }
    assert decision.merge is False
    assert f"SP-A: spec acceptance selectors not all present: {_SELECTOR} (AC1)" in (
        decision.reasons
    )


def test_undeclared_pr_touching_only_unowned_code_merges_with_the_ungoverned_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo, head = _repo(tmp_path, {_UNOWNED: "Z = 2\n"})

    team = _review(repo, head, [_UNOWNED])
    facts, decision = _decide(repo, head)
    assert team.spec_covered is None
    assert team.spec_resolution["source"] == "none"
    assert f"ungoverned paths: {_UNOWNED}" in team.spec_report
    assert decision.merge is True, decision.reasons

    monkeypatch.chdir(repo)
    evaluation = GateEvaluation(
        head_sha=head,
        ci_gate=CIGateOutcome(green=True, outcome_tag="", reason=""),
        facts=facts,
        decision=decision,
        pull_request=PullRequest(number=9, head_ref=_BRANCH, base_ref="develop", body=_BODY),
        changed_files=[_UNOWNED],
    )
    with patch("repoach.cli.review_cmds.evaluate_merge_gate", return_value=evaluation):
        result = CliRunner().invoke(review_app, ["gate", "9"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["merge"] is True
    assert payload["spec_resolution"] == {
        "source": "none",
        "specs": [],
        "ungoverned": [_UNOWNED],
    }
    assert f"ungoverned paths: {_UNOWNED}" in result.stderr.splitlines()


def test_ownership_is_read_at_base(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path, {_OWNED: "A = 2\n", _SPEC_PATH: _spec("src/elsewhere.py")})

    grading = coverage_rows_at_head(
        repo, spec_id=None, base_ref="develop", changed_files=[_OWNED, _SPEC_PATH]
    )

    assert grading.source == "ownership"
    assert [row.spec_id for row in grading.rows] == ["SP-A"]
    assert grading.rows[0].resolution == "ownership"
    assert grading.rows[0].covered is False
    assert grading.ownership is not None
    assert grading.ownership.specs == {"SP-A": [_OWNED]}
