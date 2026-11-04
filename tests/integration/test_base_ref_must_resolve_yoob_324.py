"""Yoob B-221 replayed: a depth-limited clone whose base is not fetched refuses ``review pr``.

``324`` in the file name is the Yoob merge request the defect was measured on;
B-221 is its row in Yoob's ``docs/BACKLOG.md``.

On Yoob !324 (head 48be8b8a, review:gate job 17681184626) the job clone
had depth 20 and ``develop`` was not fetched. ``review pr`` could not
find the merge base, went on with the base's own name, and the log read
``evidence of base b9146e8, reviewing base origin/``. Here the harness
origin's ``main`` advances past the fork point, the review runs from a
depth-1 clone of the PR head from which every ref of ``main`` is
removed: the review fetches ``origin/main`` itself (the test asserts the
ref is back), finds no merge base within the clone's depth, and exits
10 naming the shallow clone, before any reviewer runs, any finding is
recorded or any forge write is attempted.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.cli.refusals import EXIT_BASE_UNRESOLVABLE
from repoach.review.base_ref import SHALLOW_HINT
from repoach.review.findings import fetch_findings_if_recorded
from repoach.review.orchestrator import run_review
from tests.fixtures.generated_artefacts.harness import Forge, Repo, git, install

PR = 7


def _depth_one_clone_of_the_head(repo: Repo, tmp_path: Path) -> Path:
    """Clone the PR head at depth 1 and drop every ref of ``main``, as the Yoob job held it."""
    clone = tmp_path / "job"
    git(tmp_path, "clone", "-q", "--depth", "1", f"file://{repo.origin}", str(clone))
    git(clone, "fetch", "-q", "--depth", "1", "origin", f"+refs/pull/{PR}/head:refs/pull/{PR}/head")
    git(clone, "checkout", "-q", "--detach", f"refs/pull/{PR}/head")
    git(clone, "branch", "-q", "-D", "main")
    git(clone, "update-ref", "-d", "refs/remotes/origin/main")
    return clone


def test_a_shallow_clone_without_the_base_is_refused_before_any_review_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = install(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"}, pr=PR)
    (repo.seed / "README.md").write_text("main moved on\n", encoding="utf-8")
    git(repo.seed, "add", "-A")
    git(repo.seed, "commit", "-q", "-m", "main moves past the fork point")
    git(repo.seed, "push", "-q", "origin", "main")
    repo.checkout = _depth_one_clone_of_the_head(repo, tmp_path)
    assert git(repo.checkout, "rev-parse", "--is-shallow-repository") == "true"
    forge = Forge(repo, PR, head)
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(review_cmds, "run_review", functools.partial(run_review, forge=forge))
    capsys.readouterr()

    with pytest.raises(typer.Exit) as exited:
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

    out, err = capsys.readouterr()
    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    assert json.loads(out) == {
        "pr_number": PR,
        "error": "base_ref_unresolvable",
        "ref": "origin/main",
        "hint": SHALLOW_HINT,
    }
    assert (
        f"repoach: base ref 'origin/main' does not resolve: no merge base with HEAD; {SHALLOW_HINT}"
        in err.splitlines()
    )
    assert "reviewing base origin/" not in err
    assert git(repo.checkout, "rev-parse", "--verify", "refs/remotes/origin/main")
    assert bench.prompts.diffs == []
    assert fetch_findings_if_recorded(bench.db_path, PR) == []
    assert forge.barrier.refused == []
