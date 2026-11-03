"""SP-OPEN-THREADS-BLOCK-THE-MERGE end to end: an open thread holds a green PR until it is resolved.

A real bare origin carries ``refs/pull/7/head``; the four stub reviewers
approve it, so ``review pr --dry-run`` through the real
:class:`~repoach.forge.github.GitHubForge` (stubbed ``gh``) records a
complete review at the head. One review thread is open on the forge.
The review reports it, and the merge decision at that head, CI green,
refuses with exactly ``1 unresolved review thread(s)``. The thread is
then resolved on the forge, and the next decision merges; once the
forge's thread read fails, the decision refuses again, naming why.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult
from repoach.review.auto_merge import decide_at_head
from repoach.review.orchestrator import run_review
from tests.fixtures.generated_artefacts.harness import Forge, Repo, Review, install
from tests.forge_fakes import thread_node

PR = 7


class FlakyForge(Forge):
    """The harness forge, whose thread reads fail once ``fail_threads`` is set."""

    def __init__(self, repo: Repo, pr: int, head: str) -> None:
        super().__init__(repo, pr, head)
        self.fail_threads = False

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if self.fail_threads and args[:2] == ["api", "graphql"]:
            return GhResult(1, "", "gh: Something went wrong (HTTP 502)", argv=args)
        return super()._run(args, input_data=input_data)


def _review(
    repo: Repo, forge: Forge, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> Review:
    """Run ``review pr 7 --dry-run`` from the checkout through ``forge``."""
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(review_cmds, "run_review", functools.partial(run_review, forge=forge))
    exit_code = 0
    capsys.readouterr()
    try:
        review_cmds.review_pr(
            PR,
            dry_run=True,
            forge_name=None,
            spec=None,
            provider=None,
            keep_tree=False,
            force=False,
            dry_run_reason="dry run",
            generated_evidence=None,
        )
    except typer.Exit as exited:
        exit_code = exited.exit_code
    out, err = capsys.readouterr()
    return Review(json.loads(out), err.splitlines(), exit_code)


def test_an_open_thread_refuses_a_green_pr_until_it_is_resolved(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Approved, CI green, one open thread: refused with the exact reason; resolved: merges."""
    bench = install(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"})
    forge = FlakyForge(repo, PR, head)
    thread = thread_node(9001, resolved=False, path="src/app.py", line=2)
    forge.threads = [thread]

    review = _review(repo, forge, monkeypatch, capsys)

    assert review.exit_code == 0
    assert review.payload["final_verdict"] == "APPROVE"
    assert review.payload["review_threads"]["unresolved"] == 1
    assert review.payload["review_threads"]["threads"] == [
        {"root_id": 9001, "thread_id": None, "path": "src/app.py", "line": 2}
    ]
    assert "review threads: 1 unresolved" in review.stderr

    _, facts, decision = decide_at_head(
        PR, forge=forge, db=bench.db_path, ci_green=True, repo_root=repo.checkout, head_sha=head
    )
    assert facts.review_complete is True
    assert facts.unresolved_threads == 1
    assert decision.merge is False
    assert decision.reasons == ["1 unresolved review thread(s)"]

    thread.update(isResolved=True, resolvedBy={"login": "jwfaye"})

    _, facts, decision = decide_at_head(
        PR, forge=forge, db=bench.db_path, ci_green=True, repo_root=repo.checkout, head_sha=head
    )
    assert facts.unresolved_threads == 0
    assert facts.threads_read is True
    assert decision.merge is True
    assert decision.reasons == []

    forge.fail_threads = True
    _, facts, decision = decide_at_head(
        PR, forge=forge, db=bench.db_path, ci_green=True, repo_root=repo.checkout, head_sha=head
    )
    assert facts.threads_read is False
    assert decision.merge is False
    assert decision.reasons == ["review threads unreadable: gh: Something went wrong (HTTP 502)"]
