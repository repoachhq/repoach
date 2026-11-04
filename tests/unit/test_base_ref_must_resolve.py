"""A base ref that does not resolve is refused early, by name (Yoob B-221).

On Yoob !324 the review job's clone had depth 20 and the target branch
was not fetched: the merge base could not be found, the base became
its own name (``origin/develop``, printed ``origin/``) and the review
ran against it. :func:`~repoach.review.base_ref.resolve_base_ref`
and :func:`~repoach.review.base_ref.require_merge_base` now name the
cause (absent, ambiguous, shallow) and every command path stops on it
with exit 10 before any review work.
"""

from __future__ import annotations

import ast
import contextlib
import functools
import json
import subprocess
from collections.abc import Iterator, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import pytest
import typer

from repoach.cli import release_cmds, review_cmds
from repoach.cli.refusals import EXIT_BASE_UNRESOLVABLE, base_ref_refusal
from repoach.forge.github import GhResult
from repoach.forge.protocol import PullRequest
from repoach.review import auto_merge, coder_findings, generated_verify, orchestrator
from repoach.review.auto_merge import CIGateOutcome, evaluate_merge_gate
from repoach.review.base_ref import (
    SHALLOW_HINT,
    BaseRefKind,
    BaseRefUnresolvableError,
    require_merge_base,
    require_pr_base,
    resolve_base_ref,
)
from repoach.review.generated_evidence import load_review_evidence
from repoach.review.generated_types import GeneratedEntry
from repoach.review.orchestrator import run_review
from repoach.review.release_gate import ProvenanceSource, gather_release_facts
from tests.conftest import BASE_RESOLVERS
from tests.fixtures.generated_artefacts.harness import GIT_ENV, Forge, Repo, git, install


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
    git(clone, "update-ref", "-d", "refs/remotes/origin/develop")
    fetched: list[str] = []

    def fetch(branch: str) -> None:
        fetched.append(branch)
        git(clone, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")

    assert require_pr_base(clone, "develop", "HEAD", fetch_base=fetch) == (
        "refs/remotes/origin/develop",
        hist.develop,
        hist.fork_point,
    )
    assert fetched == ["develop"]


def test_require_pr_base_reads_the_branch_never_a_tag_of_its_name(tmp_path: Path) -> None:
    """A tag named like the base (pushed by a PR author, say) never becomes the base tip."""
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    git(clone, "tag", "develop", "HEAD")

    resolved = require_pr_base(
        clone, "develop", "HEAD", fetch_base=lambda _b: pytest.fail("fetched")
    )

    assert resolved.ref == "refs/remotes/origin/develop"
    assert resolved.tip == hist.develop


def test_require_pr_base_refuses_when_only_a_tag_has_the_base_name(tmp_path: Path) -> None:
    """With no branch of that name in the clone, a same-named tag is not a base."""
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)
    git(clone, "tag", "develop", "HEAD")
    fetched: list[str] = []

    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_pr_base(clone, "develop", "HEAD", fetch_base=fetched.append)

    assert fetched == ["develop"]
    assert refused.value.kind is BaseRefKind.ABSENT
    assert refused.value.ref == "origin/develop"


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
        "repoach: base ref 'refs/remotes/origin/develop' does not resolve: no merge base with "
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

    assert refused.value.ref == "refs/remotes/origin/develop"
    assert refused.value.hint == SHALLOW_HINT
    assert forge.calls == []

    monkeypatch.setattr(auto_merge, "select_forge", lambda **_k: forge)
    monkeypatch.setattr(review_cmds, "project_root_or_exit", lambda: clone)
    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "state" / "review.db"))
    monkeypatch.setattr("repoach.core.config._settings", None)
    monkeypatch.chdir(clone)
    capsys.readouterr()
    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_gate(7, forge_name=None, spec=None, provider=None, keep_tree=False)

    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    out, err = capsys.readouterr()
    assert json.loads(out) == {
        "pr_number": 7,
        "error": "base_ref_unresolvable",
        "ref": "refs/remotes/origin/develop",
        "hint": SHALLOW_HINT,
    }
    assert f"repoach: {refused.value}" in err.splitlines()


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

    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "state" / "review.db"))
    monkeypatch.setattr("repoach.core.config._settings", None)
    monkeypatch.setattr(release_cmds, "select_forge", lambda **_k: forge)
    monkeypatch.setattr(release_cmds, "_repo_root", lambda: clone)
    capsys.readouterr()
    with pytest.raises(typer.Exit) as exited:
        release_cmds.release_gate(pr_number=None, provenance=ProvenanceSource.GITHUB)

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


class _GoneBaseForge(Forge):
    """The harness forge, reporting a base branch the origin does not have."""

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:2] == ["pr", "view"]:
            payload = {"headRefName": "feat/x", "baseRefName": "gone", "state": "OPEN"}
            return GhResult(0, json.dumps(payload), "", argv=args)
        return super()._run(args, input_data=input_data)


def test_review_pr_refuses_an_absent_base_before_any_reviewer_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    bench = install(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"})
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(
        review_cmds,
        "run_review",
        functools.partial(run_review, forge=_GoneBaseForge(repo, 7, head)),
    )
    capsys.readouterr()

    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_pr(
            7,
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
        "pr_number": 7,
        "error": "base_ref_unresolvable",
        "ref": "origin/gone",
        "hint": "fetch it first: git fetch origin gone",
    }
    assert (
        "repoach: base ref 'origin/gone' does not resolve: absent from the clone; "
        "fetch it first: git fetch origin gone"
    ) in err.splitlines()
    assert bench.prompts.diffs == []


def test_require_pr_base_refuses_when_the_fetch_does_not_bring_the_branch(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)
    fetched: list[str] = []

    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_pr_base(clone, "develop", "HEAD", fetch_base=fetched.append)

    assert fetched == ["develop"]
    assert str(refused.value) == (
        "base ref 'origin/develop' does not resolve: absent from the clone; "
        "fetch it first: git fetch origin develop"
    )


@pytest.mark.parametrize("stdout", ["", "develop\n", "b9146e8\n"])
def test_resolve_base_ref_refuses_output_that_is_not_a_full_sha(
    tmp_path: Path, stdout: str
) -> None:
    """A git that exits 0 without a full object name never yields a base."""

    def reader(_args: object) -> tuple[int, str, str]:
        return 0, stdout, ""

    with pytest.raises(BaseRefUnresolvableError) as refused:
        resolve_base_ref(tmp_path, "develop", git=reader)

    assert refused.value.cause == f"git named no commit ({stdout.strip()!r})"
    assert refused.value.hint == "fetch it first: git fetch origin develop"


def test_review_pr_shuts_the_head_guard_down_when_the_base_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unpinned posting run submits the head guard first; the refusal shuts its pool down."""
    bench = install(monkeypatch, tmp_path)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"})
    shutdowns: list[dict[str, bool]] = []

    class RecordingPool(ThreadPoolExecutor):
        def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
            shutdowns.append({"wait": wait, "cancel_futures": cancel_futures})
            super().shutdown(wait=wait, cancel_futures=cancel_futures)

    monkeypatch.setattr(orchestrator, "ThreadPoolExecutor", RecordingPool)
    monkeypatch.setattr(orchestrator, "resolve_fresh_head", lambda *_a, **_k: head)
    team = orchestrator.ReviewTeamOrchestrator(
        forge=_GoneBaseForge(repo, 7, head),
        db_path=bench.db_path,
        post_to_github=True,
        repo_root=repo.checkout,
        config_root=repo.checkout,
    )

    with pytest.raises(BaseRefUnresolvableError, match="absent from the clone"):
        team.review_pr(7)

    assert shutdowns == [{"wait": False, "cancel_futures": True}]
    assert bench.prompts.diffs == []


def test_require_merge_base_refuses_unrelated_histories_in_a_full_clone(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    git(clone, "checkout", "-q", "--orphan", "island")
    git(clone, "commit", "-q", "--allow-empty", "-m", "unrelated")

    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_merge_base(clone, "origin/develop", "HEAD")

    assert refused.value.kind is BaseRefKind.UNRELATED
    assert str(refused.value) == (
        "base ref 'origin/develop' does not resolve: no common ancestor with HEAD; "
        "fetch it first: git fetch origin develop, and check that the head descends from it"
    )


def test_require_merge_base_refuses_an_option_like_head(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_merge_base(clone, "origin/develop", "-x")

    assert refused.value.kind is BaseRefKind.HEAD_OPTION_LIKE
    assert refused.value.ref == "origin/develop"


def test_review_fix_exits_10_with_the_json_when_the_base_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    refusal = BaseRefUnresolvableError(
        "origin/develop",
        SHALLOW_HINT,
        kind=BaseRefKind.SHALLOW,
        cause="no merge base with HEAD",
    )

    def raise_refusal(*_a: object, **_k: object) -> None:
        raise refusal

    monkeypatch.setattr(review_cmds, "run_coder_fix_from_findings", raise_refusal)
    monkeypatch.setattr(review_cmds, "project_root_or_exit", lambda: tmp_path)
    capsys.readouterr()

    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_fix(7, forge_name=None, spec=None)

    assert exited.value.exit_code == EXIT_BASE_UNRESOLVABLE
    out, err = capsys.readouterr()
    assert json.loads(out) == {"pr_number": 7, **refusal.as_json()}
    assert err.strip() == f"repoach: {refusal}"


def test_an_ambiguous_ref_is_refused_whatever_the_language_of_git(tmp_path: Path) -> None:
    """A localised warning carries no English word; the ambiguity is read from the refs."""
    calls: list[list[str]] = []

    def french_git(args: Sequence[str]) -> tuple[int, str, str]:
        argv = list(args)
        calls.append(argv)
        if argv[0] == "for-each-ref":
            return 0, "refs/heads/develop\nrefs/tags/develop\n", ""
        return 0, "a" * 40 + "\n", "avertissement : le nom de référence 'develop' est ambigu.\n"

    with pytest.raises(BaseRefUnresolvableError) as refused:
        resolve_base_ref(tmp_path, "develop", git=french_git)

    assert refused.value.kind is BaseRefKind.AMBIGUOUS
    assert calls[1][:2] == ["for-each-ref", "--format=%(refname)"]


def test_the_generated_scope_never_swallows_a_base_refusal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The broad fail-closed catch of _generated_scope re-raises the base refusal by name."""
    refusal = BaseRefUnresolvableError(
        "origin/develop", SHALLOW_HINT, kind=BaseRefKind.SHALLOW, cause="no merge base with HEAD"
    )

    def refuse(*_a: object, **_k: object) -> None:
        raise refusal

    monkeypatch.setattr(orchestrator, "review_scope", refuse)
    repo = Repo(tmp_path / "repo", {"src/app.py": "def f() -> int:\n    return 1\n"})
    head = repo.push_head({"src/app.py": "def f() -> int:\n    return 2\n"})
    team = orchestrator.ReviewTeamOrchestrator(
        forge=Forge(repo, 7, head),
        db_path=tmp_path / "review.db",
        post_to_github=False,
        repo_root=repo.checkout,
        config_root=repo.checkout,
    )

    with pytest.raises(BaseRefUnresolvableError) as raised:
        team._generated_scope("", "develop")

    assert raised.value is refusal


def test_require_merge_base_reports_the_base_before_an_option_like_head(tmp_path: Path) -> None:
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        require_merge_base(clone, "origin/develop", "-x")

    assert refused.value.kind is BaseRefKind.ABSENT


class _DecisionReachedError(Exception):
    """Raised by a stand-in for ``decide_at_head``: the gate went past the base check."""


def test_review_gate_without_a_pr_base_goes_to_the_decision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PR the forge reports without a base branch is not refused for it: there is no base to read."""
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)
    pr = PullRequest(number=7, head_sha=hist.feat, head_ref="feat", base_ref="")
    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: pr)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (hist.feat, ""))
    monkeypatch.setattr(auto_merge, "resolve_required_checks", lambda *_a, **_k: ([], {}))
    monkeypatch.setattr(
        auto_merge,
        "evaluate_ci_gate",
        lambda *_a, **_k: CIGateOutcome(green=True, outcome_tag="GREEN", reason=""),
    )
    monkeypatch.setattr(auto_merge, "materialise_head", lambda *_a, **_k: _tree_at(clone))

    def reached(*_a: object, **_k: object) -> None:
        raise _DecisionReachedError

    monkeypatch.setattr(auto_merge, "decide_at_head", reached)
    forge = _GitOnlyForge(clone)

    with pytest.raises(_DecisionReachedError):
        evaluate_merge_gate(
            7, forge=forge, db_path=tmp_path / "state" / "review.db", repo_root=clone, pin_tree=True
        )

    assert forge.calls == []


REAL_GIT_ONLY = frozenset({"repoach.review.generated_artefacts", "repoach.review.generated_verify"})
"""Importers of the resolution reached only on a real clone, so never replaced by ``base_resolves``."""


def test_base_resolves_replaces_every_importer_of_the_resolution() -> None:
    """A new call site of the base resolution fails here until it is listed."""
    src = Path(__file__).resolve().parents[2] / "src"
    importers: set[str] = set()
    for path in (src / "repoach").rglob("*.py"):
        module = ".".join(path.relative_to(src).with_suffix("").parts)
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("base_ref"):
                for alias in node.names:
                    if alias.name in {
                        "require_forge_pr_base",
                        "require_pr_base",
                        "require_merge_base",
                    }:
                        importers.add(f"{module}.{alias.name}")

    listed = set(BASE_RESOLVERS)
    unlisted = {name for name in importers - listed if name.rsplit(".", 1)[0] not in REAL_GIT_ONLY}
    assert unlisted == set()
    assert listed <= importers


def test_release_gate_refuses_a_release_branch_absent_from_the_clone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single-branch clone fetches the release branch into FETCH_HEAD only: origin/<release> is absent."""
    hist = history(tmp_path)
    clone = tmp_path / "single"
    git(
        tmp_path, "clone", "-q", "--single-branch", "--branch", "feat", str(hist.origin), str(clone)
    )
    monkeypatch.setenv("REPOACH_INTEGRATION_BRANCH", "feat")
    monkeypatch.setenv("REPOACH_RELEASE_BRANCH", "develop")
    monkeypatch.setattr("repoach.core.config._settings", None)
    forge = _GitOnlyForge(clone)

    with pytest.raises(BaseRefUnresolvableError) as refused:
        gather_release_facts(
            repo_root=clone, forge=forge, ci_runner=lambda _root: pytest.fail("CI ran")
        )

    assert refused.value.kind is BaseRefKind.ABSENT
    assert refused.value.ref == "origin/develop"
    assert not any(call[0] == "log" for call in forge.calls)


@pytest.mark.parametrize(
    ("prints_json", "pr_number", "expected"),
    [
        (False, None, None),
        (True, None, {"error": "base_ref_unresolvable", "ref": "develop", "hint": "h"}),
        (
            True,
            7,
            {"pr_number": 7, "error": "base_ref_unresolvable", "ref": "develop", "hint": "h"},
        ),
    ],
)
def test_base_ref_refusal_prints_json_only_when_the_command_does(
    capsys: pytest.CaptureFixture[str],
    prints_json: bool,
    pr_number: int | None,
    expected: dict[str, object] | None,
) -> None:
    refusal = BaseRefUnresolvableError("develop", "h", kind=BaseRefKind.ABSENT, cause="absent")

    exit_ = base_ref_refusal(refusal, prints_json=prints_json, pr_number=pr_number)

    out, err = capsys.readouterr()
    assert exit_.exit_code == EXIT_BASE_UNRESOLVABLE
    assert err.strip() == f"repoach: {refusal}"
    assert (json.loads(out) if out else None) == expected


def test_review_generated_never_reads_the_declarations_at_a_same_named_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--base origin/develop with no remote branch and a tag of that name is refused, not run."""
    hist = history(tmp_path)
    clone = shallow_clone(tmp_path, hist, fetch_develop=False)
    git(clone, "tag", "origin/develop", "HEAD")
    monkeypatch.setattr(generated_verify, "head_tree", lambda *_a, **_k: pytest.fail("ran"))

    with pytest.raises(BaseRefUnresolvableError) as refused:
        generated_verify.verify_generated(
            clone, pr_number=7, head=hist.feat, base_ref="origin/develop", state_dir=tmp_path
        )

    assert refused.value.kind is BaseRefKind.ABSENT
    assert refused.value.ref == "origin/develop"


def test_review_generated_reads_the_declarations_at_the_remote_branch_tip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the remote branch present, a same-named tag is ignored: the declarations come from its tip."""
    hist = history(tmp_path)
    clone = full_clone(tmp_path, hist)
    git(clone, "tag", "origin/develop", "HEAD")
    read_at: list[str | None] = []

    def declared(_root: Path, ref: str | None) -> list[object]:
        read_at.append(ref)
        return []

    monkeypatch.setattr(generated_verify, "declared_at_base", declared)

    payload, checks, _timing = generated_verify.verify_generated(
        clone, pr_number=7, head=hist.feat, base_ref="origin/develop", state_dir=tmp_path
    )

    assert read_at == [hist.develop]
    assert payload["base_sha"] == hist.fork_point
    assert checks == []
