"""The gate names the spec the PR head carries, not the checkout's short guess.

``repoach review gate 246`` printed ``"spec": "SP-AGENT"`` for the branch
``docs/sp-agent-context-loop``: the checkout (develop) did not carry
``SP-AGENT-CONTEXT-LOOP`` yet, so the branch parser fell back to its
short regex. The id is now resolved against the specs at the PR head and
read from the matched spec's frontmatter.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from repoach.cli.review_cmds import _branch_spec_at_head
from repoach.project.spec_source import SpecResolution
from repoach.review.governed_spec import branch_spec_id_at_ref
from repoach.review.spec import detect_spec_from_branch

_BRANCH = "docs/sp-agent-context-loop"
_SPEC = "docs/specs/2026-10-25_SP-AGENT-CONTEXT-LOOP_three-role-context.md"


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _spec_text(spec_id: str) -> str:
    return (
        "---\n"
        f"id: {spec_id}\n"
        "title: T\n"
        "version: 0.1\n"
        "status: draft\n"
        "owns:\n"
        "  code: []\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n# T\n"
    )


def _repo(tmp_path: Path) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "docs/specs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "docs/specs/2026-01-01_SP-OTHER_other.md").write_text(
        _spec_text("SP-OTHER"), encoding="utf-8"
    )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", _BRANCH)
    (repo / _SPEC).write_text(_spec_text("SP-AGENT-CONTEXT-LOOP"), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    head = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "develop")
    return repo, head


def test_checkout_without_the_spec_truncates_the_hyphenated_id(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    assert detect_spec_from_branch(_BRANCH, root=repo) == "SP-AGENT"


def test_hyphenated_id_resolves_from_the_head_frontmatter(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    assert branch_spec_id_at_ref(_BRANCH, head, root=repo) == "SP-AGENT-CONTEXT-LOOP"


def test_gate_replaces_the_checkout_guess_with_the_head_id(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path)
    guessed = SpecResolution("SP-AGENT", "branch")
    resolved = _branch_spec_at_head(guessed, branch=_BRANCH, head_sha=head, root=repo)
    assert resolved == SpecResolution("SP-AGENT-CONTEXT-LOOP", "branch")


def test_gate_keeps_the_checkout_id_when_the_head_is_unreadable(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    guessed = SpecResolution("SP-AGENT", "branch")
    resolved = _branch_spec_at_head(guessed, branch=_BRANCH, head_sha="0" * 40, root=repo)
    assert resolved == guessed


def test_head_with_no_matching_spec_keeps_the_checkout_id(tmp_path: Path) -> None:
    repo, _ = _repo(tmp_path)
    base = _git(repo, "rev-parse", "develop")
    assert branch_spec_id_at_ref(_BRANCH, base, root=repo) is None


def test_longest_known_id_wins() -> None:
    known = ("SP-AGENT", "SP-AGENT-CONTEXT-LOOP")
    assert detect_spec_from_branch(_BRANCH, known_ids=known) == "SP-AGENT-CONTEXT-LOOP"
