"""A base ref that does not resolve is refused early, by name (Yoob B-221).

On Yoob !324 the review job's clone had depth 20 and the target branch
was not fetched: the merge base could not be found, the base became
its own name (``origin/develop``, printed ``origin/``) and the review
ran against it. :func:`~repoach.review.generated_git.resolve_base_ref`
and :func:`~repoach.review.generated_git.require_merge_base` now name the
cause (absent, ambiguous, shallow) and every command path stops on it
with exit 10 before any review work.
"""

from __future__ import annotations

import contextlib
import json
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer

from repoach.cli import release_cmds, review_cmds
from repoach.forge.github import GhResult
from repoach.forge.protocol import PullRequest
from repoach.review import auto_merge, coder_findings, generated_verify
from repoach.review.auto_merge import CIGateOutcome, evaluate_merge_gate
from repoach.review.generated_evidence import load_review_evidence
from repoach.review.generated_git import (
    EXIT_BASE_UNRESOLVABLE,
    SHALLOW_HINT,
    BaseRefUnresolvableError,
    require_merge_base,
    require_pr_base,
    resolve_base_ref,
)
from repoach.review.generated_types import GeneratedEntry
from repoach.review.release_gate import gather_release_facts
from tests.fixtures.generated_artefacts.harness import Repo, install, review

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_NOSYSTEM": "1",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` with a fixed identity and return its stdout."""
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**GIT_ENV, "HOME": str(cwd)},
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


@dataclass
class History:
    """An origin whose ``develop`` and ``feat`` fork three commits back, and its clones."""

    origin: Path
    fork_point: str
    develop: str
    feat: str


def history(tmp_path: Path) -> History:
    """Build the origin: develop has 6 commits, feat forks at the 3rd and adds 3 of its own."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "develop")
    for n in range(3):
        (seed / "a.txt").write_text(f"{n}\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"develop {n}")
    fork_point = git(seed, "rev-parse", "HEAD")
    git(seed, "checkout", "-q", "-b", "feat")
    for n in range(3):
        (seed / "b.txt").write_text(f"{n}\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"feat {n}")
    feat = git(seed, "rev-parse", "HEAD")
    git(seed, "checkout", "-q", "develop")
    for n in range(3, 6):
        (seed / "a.txt").write_text(f"{n}\n", encoding="utf-8")
        git(seed, "add", "-A")
        git(seed, "commit", "-q", "-m", f"develop {n}")
    develop = git(seed, "rev-parse", "HEAD")
    origin = tmp_path / "origin.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    return History(origin=origin, fork_point=fork_point, develop=develop, feat=feat)


def full_clone(tmp_path: Path, hist: History, name: str = "full") -> Path:
    """A full clone checked out at ``feat``, ``origin/develop`` present."""
    clone = tmp_path / name
    git(tmp_path, "clone", "-q", "--branch", "feat", str(hist.origin), str(clone))
    return clone


def shallow_clone(tmp_path: Path, hist: History, *, fetch_develop: bool) -> Path:
    """A depth-1 clone of ``feat``, as a CI job clones; ``develop`` fetched at depth 1 or not at all."""
    clone = tmp_path / "shallow"
    git(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        "--single-branch",
        "--branch",
        "feat",
        f"file://{hist.origin}",
        str(clone),
    )
    if fetch_develop:
        git(clone, "fetch", "-q", "--depth", "1", "origin", "develop:refs/remotes/origin/develop")
    return clone


def test_resolve_base_ref_returns_the_full_sha_of_a_good_ref(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)

    assert resolve_base_ref(clone, "origin/develop") == hist.develop
    assert require_merge_base(clone, "origin/develop", "HEAD") == hist.fork_point


def test_resolve_base_ref_refuses_a_ref_absent_from_the_clone(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        resolve_base_ref(clone, "origin/develop")

    assert str(refused.value) == (
        "base ref 'origin/develop' does not resolve: absent from the clone; "
        "fetch it first: git fetch origin develop"
    )
    assert refused.value.as_json() == {
        "error": "base_ref_unresolvable",
        "ref": "origin/develop",
        "hint": "fetch it first: git fetch origin develop",
    }


def test_require_merge_base_refuses_a_shallow_clone_whose_depth_excludes_the_base(
    tmp_path: Path,
) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=True)

    assert resolve_base_ref(clone, "origin/develop") == hist.develop
    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_merge_base(clone, "origin/develop", "HEAD")

    assert refused.value.hint == SHALLOW_HINT
    assert str(refused.value) == (
        "base ref 'origin/develop' does not resolve: no merge base with HEAD; " + SHALLOW_HINT
    )


def test_resolve_base_ref_refuses_an_ambiguous_ref(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    git(clone, "branch", "develop", "origin/develop")
    git(clone, "tag", "develop", hist.fork_point)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        resolve_base_ref(clone, "develop")

    assert str(refused.value) == (
        "base ref 'develop' does not resolve: ambiguous, more than one ref has that name; "
        "name it in full: refs/remotes/origin/develop or refs/heads/develop"
    )


@pytest.mark.parametrize("ref", ["", "  ", "-develop"])
def test_resolve_base_ref_refuses_an_empty_or_option_like_ref(tmp_path: Path, ref: str) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        resolve_base_ref(clone, ref)

    assert refused.value.ref == ref


def test_require_pr_base_fetches_origin_once_for_an_absent_branch(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    fetched: list[str] = []

    def fetch(branch: str) -> None:
        fetched.append(branch)
        git(clone, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")

    assert require_pr_base(clone, "develop", "HEAD", fetch_base=fetch) == hist.fork_point
    assert fetched == ["develop"]


def test_require_pr_base_does_not_fetch_an_ambiguous_ref(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    git(clone, "branch", "develop", "origin/develop")
    git(clone, "tag", "develop", hist.fork_point)

    with pytest.raises(BaseRefUnresolvableError, match="ambiguous"):
        require_pr_base(clone, "develop", "HEAD", fetch_base=lambda _b: pytest.fail("fetched"))


@pytest.mark.parametrize("base_sha", ["", "origin/develop", "b9146e8"])
def test_the_evidence_check_refuses_a_base_that_is_not_a_full_sha(
    tmp_path: Path, base_sha: str
) -> None:
    """The empty string, a ref name or a short SHA never reaches the evidence comparison."""
    entry = GeneratedEntry(
        path="data/v.json", check=("scripts/check.py", "--check"), sources=("data/in.json",)
    )
    evidence = tmp_path / "review-generated.json"
    evidence.write_text("{}", encoding="utf-8")

    with pytest.raises(BaseRefUnresolvableError) as refused:
        load_review_evidence(
            evidence,
            {"data/v.json": entry},
            head_sha="f" * 40,
            head_dir=tmp_path,
            changed_files=["data/v.json"],
            base_sha=base_sha,
        )

    assert refused.value.ref == base_sha


def test_review_generated_exits_10_on_a_shallow_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=True)
    monkeypatch.chdir(clone)
    monkeypatch.setattr(generated_verify, "held_secrets", lambda: [])
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "state" / "review.db"))
    monkeypatch.setattr("repoach.core.config._settings", None)
    out = tmp_path / "review-generated.json"

    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_generated(
            pr_number=7, head=hist.feat, out=str(out), base="origin/develop"
        )

    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    _, err = capsys.readouterr()
    assert err.strip() == (
        "repoach: base ref 'origin/develop' does not resolve: no merge base with "
        f"{hist.feat[:12]}; {SHALLOW_HINT}"
    )
    assert not out.exists()


@contextlib.contextmanager
def _tree_at(root: Path) -> Iterator[object]:
    """Stand in for ``materialise_head``: the checkout is already at the head."""

    @dataclass
    class _Tree:
        root: Path
        tree: str = "checkout"

        def announcement(self, pr_number: int) -> str:
            return f"reviewing {pr_number} at head (checkout {self.root})"

    yield _Tree(root)


class _GitOnlyForge:
    """A forge whose ``git`` runs in ``root`` and records every call."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.calls: list[list[str]] = []

    def git(self, args: list[str]) -> GhResult:
        self.calls.append(list(args))
        proc = subprocess.run(
            ["git", *args],
            cwd=self.root,
            env={**GIT_ENV, "HOME": str(self.root)},
            capture_output=True,
            text=True,
            check=False,
        )
        return GhResult(proc.returncode, proc.stdout, proc.stderr, argv=["git", *args])


def test_review_gate_refuses_a_shallow_base_before_any_finding_is_reverified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=True)
    pr = PullRequest(number=7, head_sha=hist.feat, head_ref="feat", base_ref="develop")
    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: pr)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (hist.feat, ""))
    monkeypatch.setattr(auto_merge, "resolve_required_checks", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(
        auto_merge,
        "evaluate_ci_gate",
        lambda *_a, **_k: CIGateOutcome(green=True, outcome_tag="GREEN", reason=""),
    )
    monkeypatch.setattr(auto_merge, "materialise_head", lambda *_a, **_k: _tree_at(clone))
    monkeypatch.setattr(
        auto_merge, "decide_at_head", lambda *_a, **_k: pytest.fail("the gate re-verified")
    )
    forge = _GitOnlyForge(clone)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        evaluate_merge_gate(
            7, forge=forge, db_path=tmp_path / "state" / "review.db", repo_root=clone, pin_tree=True
        )

    assert refused.value.ref == "origin/develop"
    assert refused.value.hint == SHALLOW_HINT
    assert forge.calls == [
        ["fetch", "--quiet", "origin", "+refs/heads/develop:refs/remotes/origin/develop"]
    ]

    def raise_refusal(*_a: object, **_k: object) -> None:
        raise refused.value

    monkeypatch.setattr(review_cmds, "evaluate_merge_gate", raise_refusal)
    monkeypatch.chdir(clone)
    capsys.readouterr()
    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_gate(7, forge_name=None, spec=None, provider=None, keep_tree=False)

    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "pr_number": 7,
        "error": "base_ref_unresolvable",
        "ref": "origin/develop",
        "hint": SHALLOW_HINT,
    }
    assert err.strip() == f"repoach: {refused.value}"


def test_release_gate_refuses_a_release_branch_beyond_the_shallow_depth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    hist = history(tmp_path)
    clone = tmp_path / "release"
    git(
        tmp_path,
        "clone",
        "-q",
        "--depth",
        "1",
        "--no-single-branch",
        f"file://{hist.origin}",
        str(clone),
    )
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "feat")
    monkeypatch.setenv("REPOACH_RELEASE_BRANCH", "develop")
    monkeypatch.setattr("repoach.core.config._settings", None)
    forge = _GitOnlyForge(clone)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        gather_release_facts(
            repo_root=clone,
            forge=forge,
            ci_runner=lambda _root: pytest.fail("CI ran"),
        )

    assert refused.value.ref == "origin/develop"
    assert refused.value.hint == SHALLOW_HINT
    assert not any(call[0] == "log" for call in forge.calls)

    def raise_refusal(**_k: object) -> None:
        raise refused.value

    monkeypatch.setattr(release_cmds, "gather_release_facts", raise_refusal)
    monkeypatch.setattr(release_cmds, "select_forge", lambda **_k: forge)
    monkeypatch.setattr(release_cmds, "_repo_root", lambda: clone)
    capsys.readouterr()
    with pytest.raises(typer.Exit) as exited:
        release_cmds.release_gate(pr_number=None)

    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    out, err = capsys.readouterr()
    assert json.loads(out) == refused.value.as_json()
    assert err.strip() == f"repoach: {refused.value}"


class _CoderForge(_GitOnlyForge):
    """The Coder loop's forge: one PR read, real git, nothing else expected."""

    def __init__(self, root: Path, pr: PullRequest) -> None:
        super().__init__(root)
        self._pr = pr

    def pull_request(self, number: int) -> PullRequest:
        return self._pr


def test_the_coder_loop_refuses_an_absent_base_before_any_finding_is_recorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)
    git(clone, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "develop")
    monkeypatch.setattr("repoach.core.config._settings", None)
    monkeypatch.setattr(
        coder_findings, "record_ci_failures_as_findings", lambda *_a, **_k: pytest.fail("recorded")
    )
    pr = PullRequest(number=7, head_sha=hist.feat, head_ref="feat", base_ref="develop")
    forge = _CoderForge(clone, pr)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        coder_findings.run_coder_fix_from_findings(
            7, forge=forge, repo_root=clone, db_path=tmp_path / "state" / "review.db"
        )

    assert str(refused.value) == (
        "base ref 'origin/develop' does not resolve: absent from the clone; "
        "fetch it first: git fetch origin develop"
    )
    assert [call[0] for call in forge.calls] == ["fetch"]


def test_review_pr_refuses_an_ambiguous_base_before_any_reviewer_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = install(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"})
    git(repo.checkout, "tag", "main", "HEAD")

    result = review(repo, monkeypatch, capsys, head=head)

    assert result.exit_code == EXIT_BASE_UNRESOLVABLE
    assert result.payload == {
        "pr_number": 7,
        "error": "base_ref_unresolvable",
        "ref": "main",
        "hint": "name it in full: refs/remotes/origin/main or refs/heads/main",
    }
    assert (
        "repoach: base ref 'main' does not resolve: ambiguous, more than one ref has that name; "
        "name it in full: refs/remotes/origin/main or refs/heads/main"
    ) in result.stderr
    assert bench.prompts.diffs == []
