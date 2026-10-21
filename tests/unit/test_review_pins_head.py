"""SP-REVIEW-PINS-HEAD G2: a review reads the PR head tree, never the checkout's branch.

A real git origin carries ``main`` (an undocumented ``src/x.py``) and
``refs/pull/7/head`` (the same file documented). The checkout sits on
``main``; a stub Scribe claims a blocking missing docstring. Reading
the checkout would verify the claim; reading the head refutes it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from repoach.forge.github import GhResult, GitHubForge
from repoach.review.findings import FindingStatus, fetch_findings
from repoach.review.orchestrator import run_review
from repoach.review.review_head import (
    HeadUnavailableError,
    HeadUnresolvedError,
    materialise_head,
    require_head_sha,
    worktree_path,
)
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

_UNDOCUMENTED = "def f():\n    return 1\n"
_DOCUMENTED = '"""Module."""\n\n\ndef f():\n    """Return one."""\n    return 1\n'
_DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1 @@\n+x\n"


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
    }
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def _commit(repo: Path, content: str, message: str) -> str:
    (repo / "src").mkdir(exist_ok=True)
    (repo / "src" / "x.py").write_text(content, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def origin_and_checkout(tmp_path: Path) -> tuple[Path, str]:
    """Return ``(checkout on main, head SHA of refs/pull/7/head)``."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _commit(seed, _UNDOCUMENTED, "base")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main")
    head = _commit(seed, _DOCUMENTED, "document")
    _git(seed, "push", "-q", "origin", "HEAD:refs/pull/7/head")
    checkout = tmp_path / "checkout"
    _git(tmp_path, "clone", "-q", str(origin), str(checkout))
    return checkout, head


class _HeadForge(GitHubForge):
    """A :class:`GitHubForge` with a stubbed ``gh`` and a real ``git``."""

    def __init__(self, cwd: Path, head: str) -> None:
        super().__init__(cwd=cwd, gh_path="/nonexistent/gh", bot_login="bot")
        self._head = head

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:2] == ["pr", "view"]:
            return GhResult(
                0, json.dumps({"headRefName": "feat/x", "state": "OPEN"}), "", argv=args
            )
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return GhResult(0, "[[]]", "", argv=args)
        raise AssertionError(f"unexpected gh call: {args}")

    def _pr_diff(self, pr_number: int) -> GhResult:
        return GhResult(0, _DIFF, "", argv=["gh", "pr", "diff"])

    def _pr_head_sha(self, pr_number: int) -> str:
        return self._head


class _StubReviewer:
    def __init__(self, outcome: ReviewerOutcome) -> None:
        self._outcome = outcome
        self.role = outcome.role

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        return self._outcome


def _outcome(role: BotRole, comments: list[ReviewComment] | None = None) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.REQUEST_CHANGES if comments else ReviewVerdict.APPROVE,
        summary="ok",
        comments=comments or [],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="{}",
    )


@pytest.fixture
def stub_review(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Stub the reviewers and the provider check; point the ledger at ``tmp_path``."""
    scribe = _outcome(
        BotRole.SCRIBE,
        [ReviewComment(file="src/x.py", line=1, severity="blocker", body="missing docstring")],
    )
    outcomes = {
        "Architect": _outcome(BotRole.ARCHITECT),
        "Sentinel": _outcome(BotRole.SENTINEL),
        "Tester": _outcome(BotRole.TESTER),
        "Scribe": scribe,
    }
    for name, outcome in outcomes.items():
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _o=outcome, **_kw: _StubReviewer(_o),
        )
    monkeypatch.setattr("repoach.review.orchestrator.require_provider_ready", lambda: None)
    state = tmp_path / "state"
    monkeypatch.setenv("REPOACH_DB_PATH", str(state / "review.db"))
    monkeypatch.setattr("repoach.core.config._settings", None)
    return state / "review.db"


def _worktrees(checkout: Path) -> list[str]:
    return [
        line
        for line in _git(checkout, "worktree", "list", "--porcelain").splitlines()
        if line.startswith("worktree ")
    ]


def test_dry_run_reads_head_tree_not_checkout(
    monkeypatch: pytest.MonkeyPatch, origin_and_checkout: tuple[Path, str], stub_review: Path
) -> None:
    checkout, head = origin_and_checkout
    monkeypatch.chdir(checkout)
    lines: list[str] = []

    team = run_review(7, post=False, forge=_HeadForge(checkout, head), announce=lines.append)

    findings = fetch_findings(stub_review, 7)
    assert [(f.claim_type.value, f.status) for f in findings] == [
        ("missing_docstring", FindingStatus.REFUTED)
    ]
    assert {f.head_sha for f in findings} == {head}
    assert team.head_sha == head
    assert team.tree == "worktree"
    assert team.worktree is None
    path = worktree_path(stub_review.parent, 7, head)
    assert lines == [f"reviewing 7 at {head[:7]} (worktree {path})"]
    assert not path.exists()
    assert len(_worktrees(checkout)) == 1
    assert _git(checkout, "branch", "--show-current") == "main"
    assert (checkout / "src" / "x.py").read_text(encoding="utf-8") == _UNDOCUMENTED


def test_keep_tree_leaves_the_worktree_and_names_it(
    monkeypatch: pytest.MonkeyPatch, origin_and_checkout: tuple[Path, str], stub_review: Path
) -> None:
    checkout, head = origin_and_checkout
    monkeypatch.chdir(checkout)

    team = run_review(7, post=False, forge=_HeadForge(checkout, head), keep_tree=True)

    path = worktree_path(stub_review.parent, 7, head)
    assert team.worktree == str(path)
    assert (path / "src" / "x.py").read_text(encoding="utf-8") == _DOCUMENTED
    _git(checkout, "worktree", "remove", "--force", str(path))


def test_checkout_at_head_and_clean_is_reviewed_in_place(
    monkeypatch: pytest.MonkeyPatch, origin_and_checkout: tuple[Path, str], stub_review: Path
) -> None:
    checkout, head = origin_and_checkout
    _git(checkout, "fetch", "-q", "origin", "refs/pull/7/head")
    _git(checkout, "checkout", "-q", head)
    monkeypatch.chdir(checkout)
    lines: list[str] = []

    team = run_review(7, post=False, forge=_HeadForge(checkout, head), announce=lines.append)

    assert team.tree == "checkout"
    assert lines == [f"reviewing 7 at {head[:7]} (checkout {checkout})"]
    assert [f.status for f in fetch_findings(stub_review, 7)] == [FindingStatus.REFUTED]


def test_dirty_checkout_at_head_still_uses_a_worktree(
    origin_and_checkout: tuple[Path, str], tmp_path: Path
) -> None:
    checkout, head = origin_and_checkout
    _git(checkout, "fetch", "-q", "origin", "refs/pull/7/head")
    _git(checkout, "checkout", "-q", head)
    (checkout / "src" / "x.py").write_text("uncommitted = 1\n", encoding="utf-8")

    with materialise_head(
        _HeadForge(checkout, head), 7, head, repo_root=checkout, state_dir=tmp_path / "s"
    ) as tree:
        assert tree.tree == "worktree"
        assert (tree.root / "src" / "x.py").read_text(encoding="utf-8") == _DOCUMENTED
    assert not tree.root.exists()


@pytest.mark.parametrize("served", [None, "", "deadbeef", "Z" * 40])
def test_head_that_is_not_a_full_sha_is_refused(served: str | None) -> None:
    with pytest.raises(HeadUnresolvedError, match="cannot resolve head of PR 7"):
        require_head_sha(served, 7)


def test_head_absent_from_the_fetched_ref_is_unavailable(
    origin_and_checkout: tuple[Path, str], tmp_path: Path
) -> None:
    checkout, _head = origin_and_checkout
    ghost = "0" * 40

    with (
        pytest.raises(HeadUnavailableError, match="does not contain"),
        materialise_head(
            _HeadForge(checkout, ghost), 7, ghost, repo_root=checkout, state_dir=tmp_path / "s"
        ),
    ):
        pytest.fail("no tree may be yielded")


def test_gate_re_verifies_on_the_head_tree(
    monkeypatch: pytest.MonkeyPatch, origin_and_checkout: tuple[Path, str], tmp_path: Path
) -> None:
    from repoach.review import auto_merge
    from repoach.review.merge_gate import MergeDecision

    checkout, head = origin_and_checkout
    seen: dict[str, str] = {}

    def _decide(pr_number: int, **kwargs: object):
        root = kwargs["repo_root"]
        assert isinstance(root, Path)
        seen["x"] = (root / "src" / "x.py").read_text(encoding="utf-8")
        return head, auto_merge._unknown_facts(), MergeDecision(merge=False, reasons=["stub"])

    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: None)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (head, ""))
    monkeypatch.setattr(
        auto_merge,
        "evaluate_ci_gate",
        lambda *_a, **_k: auto_merge.CIGateOutcome(green=True, outcome_tag="GREEN", reason=""),
    )
    monkeypatch.setattr(auto_merge, "decide_at_head", _decide)
    lines: list[str] = []

    evaluation = auto_merge.evaluate_merge_gate(
        7,
        forge=_HeadForge(checkout, head),
        db_path=tmp_path / "gate" / "review.db",
        repo_root=checkout,
        pin_tree=True,
        announce=lines.append,
    )

    assert seen["x"] == _DOCUMENTED
    assert evaluation.tree == "worktree"
    path = worktree_path(tmp_path / "gate", 7, head)
    assert lines == [f"reviewing 7 at {head[:7]} (worktree {path})"]
    assert not path.exists()
