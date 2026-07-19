"""Unit tests for SP-DEVAGENT-WIRE — the parent-supersession lifecycle helper.

Real git repos in tmp (init + ``git rm`` are exercised for real). Pins: the parent
spec is removed from index and working tree; a path escaping ``docs/specs/`` is
refused; a git failure returns an error string without raising; and the empty-sub-specs
guard refuses to delete.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from repoach.review.spec import SpecPlan
from repoach.review.spec_supersede import supersede_parent_on_decompose


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo(tmp_path: Path) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    (repo / "docs" / "specs").mkdir(parents=True)
    parent_rel = Path("docs/specs/2026-06-28_SP-DEMO_parent.md")
    (repo / parent_rel).write_text("---\nid: SP-DEMO\n---\n\n# Parent\n", encoding="utf-8")
    (repo / "README.md").write_text("demo\n", encoding="utf-8")
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "chore: init")
    return repo, parent_rel


def _parent_spec(file_path: Path) -> SpecPlan:
    return SpecPlan(
        id="SP-DEMO",
        file_path=file_path,
        raw_markdown="# Parent\n",
        title="Parent",
        summary="the parent spec",
    )


def _stage_subspec(repo: Path) -> Path:
    sub = repo / "docs" / "specs" / "2026-06-28_SP-DEMO-1_sub.md"
    sub.write_text("---\nid: SP-DEMO-1\n---\n\n# Sub\n", encoding="utf-8")
    _git(repo, "add", "--", str(sub.relative_to(repo)))
    return sub


def test_supersede_removes_parent_from_index_and_tree(tmp_path: Path) -> None:
    repo, parent_rel = _init_repo(tmp_path)
    sub = _stage_subspec(repo)

    error = supersede_parent_on_decompose(repo, _parent_spec(parent_rel), staged_subspecs=[sub])

    assert error == ""
    assert not (repo / parent_rel).exists()
    tracked = _git(repo, "ls-files").splitlines()
    assert str(parent_rel) not in tracked


def test_supersede_refuses_path_outside_specs(tmp_path: Path) -> None:
    repo, _ = _init_repo(tmp_path)
    sub = _stage_subspec(repo)
    outside = Path("README.md")

    error = supersede_parent_on_decompose(repo, _parent_spec(outside), staged_subspecs=[sub])

    assert error and "docs/specs" in error
    assert (repo / outside).exists()


def test_supersede_git_failure_returns_error(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain"
    (not_a_repo / "docs" / "specs").mkdir(parents=True)
    parent_rel = Path("docs/specs/2026-06-28_SP-DEMO_parent.md")
    (not_a_repo / parent_rel).write_text("# Parent\n", encoding="utf-8")
    sub = not_a_repo / "docs" / "specs" / "sub.md"
    sub.write_text("# Sub\n", encoding="utf-8")

    error = supersede_parent_on_decompose(
        not_a_repo, _parent_spec(parent_rel), staged_subspecs=[sub]
    )

    assert error and "git rm" in error
    assert (not_a_repo / parent_rel).exists()


def test_supersede_empty_subspecs_refuses(tmp_path: Path) -> None:
    repo, parent_rel = _init_repo(tmp_path)

    error = supersede_parent_on_decompose(repo, _parent_spec(parent_rel), staged_subspecs=[])

    assert error and "no sub-specs" in error
    assert (repo / parent_rel).exists()
