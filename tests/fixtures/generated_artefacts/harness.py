"""A real git origin, a stubbed forge, stub reviewers and a recording runner.

Shared by the generated-artefact tests (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ):
a seed clone pushes each PR head to ``refs/pull/<n>/head`` of a bare
origin, the forge serves the three-dot diff of that head against
``main``, the four reviewers approve without a comment while recording
every diff they were shown, and the refuter judge counts its calls.
``review pr`` and ``review generated`` run through their CLI functions.
"""

from __future__ import annotations

import functools
import json
import os
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult, GitHubForge
from repoach.review import generated_verify, orchestrator
from repoach.review.generated_process import RunResult, subprocess_runner
from repoach.review.head_reverify import reverify_older_findings
from repoach.review.orchestrator import run_review
from repoach.review.refuter import judge_findings_for_pr
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict

GIT_ENV = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
    "PATH": "/usr/bin:/bin:/usr/local/bin",
}


def git(cwd: Path, *args: str) -> str:
    """Run git in ``cwd`` with a fixed identity and return its stdout."""
    env = {**GIT_ENV, "HOME": str(cwd)}
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class Repo:
    """A bare origin, a seed clone that pushes PR heads, and a checkout of ``main``."""

    def __init__(self, tmp_path: Path, files: Mapping[str, str]) -> None:
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.origin = tmp_path / "origin.git"
        git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        git(self.seed, "init", "-q", "-b", "main")
        self._write(files)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", "base")
        git(self.seed, "remote", "add", "origin", str(self.origin))
        git(self.seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        git(tmp_path, "clone", "-q", str(self.origin), str(self.checkout))
        self.diffs: dict[int, str] = {}

    def _write(self, files: Mapping[str, str | None]) -> None:
        for relpath, content in files.items():
            target = self.seed / relpath
            if content is None:
                target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    def push_head(self, files: Mapping[str, str | None], *, pr: int = 7) -> str:
        """Commit ``files`` on the PR's branch, push it as the PR head, return its SHA."""
        branch = f"pr-{pr}"
        if git(self.seed, "branch", "--list", branch):
            git(self.seed, "checkout", "-q", branch)
        else:
            git(self.seed, "checkout", "-q", "-b", branch)
        self._write(files)
        git(self.seed, "add", "-A")
        git(self.seed, "commit", "-q", "-m", f"head of {pr}")
        git(self.seed, "push", "-q", "-f", "origin", f"HEAD:refs/pull/{pr}/head")
        self.diffs[pr] = git(self.seed, "diff", "main...HEAD") + "\n"
        sha = git(self.seed, "rev-parse", "HEAD")
        git(self.seed, "checkout", "-q", "main")
        return sha

    def merge(self, pr: int) -> None:
        """Merge the PR's branch into ``main`` on the origin and pull it into the checkout."""
        git(self.seed, "merge", "-q", "--no-edit", f"pr-{pr}")
        git(self.seed, "push", "-q", "origin", "main")
        git(self.checkout, "pull", "-q", "origin", "main")


class Forge(GitHubForge):
    """A :class:`GitHubForge` with a stubbed ``gh`` and a real ``git``."""

    def __init__(self, repo: Repo, pr: int, head: str) -> None:
        super().__init__(cwd=repo.checkout, gh_path="/nonexistent/gh", bot_login="bot")
        self._repo = repo
        self._pr = pr
        self._head = head

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:2] == ["pr", "view"]:
            payload = {"headRefName": "feat/x", "baseRefName": "main", "state": "OPEN"}
            return GhResult(0, json.dumps(payload), "", argv=args)
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return GhResult(0, "[[]]", "", argv=args)
        raise AssertionError(f"unexpected gh call: {args}")

    def _pr_diff(self, pr_number: int) -> GhResult:
        return GhResult(0, self._repo.diffs[self._pr], "", argv=["gh", "pr", "diff"])

    def _pr_head_sha(self, pr_number: int) -> str:
        return self._head


class Judge:
    """A refuter judge that confirms every finding and counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        return f"VERDICT: {json.dumps({'refuted': False, 'reasoning': ''})}"


@dataclass
class Prompts:
    """Every diff a reviewer was shown."""

    diffs: list[str] = field(default_factory=list)


class _ApprovingReviewer:
    def __init__(self, role: BotRole, prompts: Prompts) -> None:
        self.role = role
        self._prompts = prompts

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        self._prompts.diffs.append(diff)
        return ReviewerOutcome(
            role=self.role,
            verdict=ReviewVerdict.APPROVE,
            summary="ok",
            comments=[],
            model_used="stub",
            elapsed_s=0.0,
            tokens_used=0,
            raw_response="{}",
        )


@dataclass
class Bench:
    """The stubs one test drives."""

    judge: Judge
    prompts: Prompts
    db_path: Path


def install(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Bench:
    """Stub the reviewers, the provider check and the judge; point the ledger at ``tmp_path``."""
    prompts = Prompts()
    roles = {
        "Architect": BotRole.ARCHITECT,
        "Sentinel": BotRole.SENTINEL,
        "Tester": BotRole.TESTER,
        "Scribe": BotRole.SCRIBE,
    }
    for name, role in roles.items():
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _r=role, **_kw: _ApprovingReviewer(_r, prompts),
        )
    monkeypatch.setattr("repoach.review.orchestrator.require_provider_ready", lambda: None)
    db_path = tmp_path / "state" / "review.db"
    monkeypatch.setenv("REPOACH_DB_PATH", str(db_path))
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)
    monkeypatch.setattr("repoach.core.config._settings", None)
    judge = Judge()
    monkeypatch.setattr(
        orchestrator,
        "judge_findings_for_pr",
        functools.partial(judge_findings_for_pr, judge_factory=lambda: judge),
    )
    monkeypatch.setattr(
        orchestrator,
        "reverify_older_findings",
        functools.partial(reverify_older_findings, judge_factory=lambda: judge),
    )
    return Bench(judge=judge, prompts=prompts, db_path=db_path)


@dataclass
class Call:
    """One runner call, with the check script's content at call time."""

    argv: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_s: float
    shell: bool
    script: str


class RecordingRunner:
    """Records every call, then runs it for real (or returns ``scripted`` results in order)."""

    def __init__(self, scripted: Sequence[RunResult] = ()) -> None:
        self.calls: list[Call] = []
        self.reexecs: list[tuple[str, list[str], dict[str, str]]] = []
        self._scripted = list(scripted)

    def __call__(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        shell: bool = False,
    ) -> RunResult:
        script_path = cwd / argv[1] if len(argv) > 1 and (cwd / argv[1]).is_file() else None
        script = script_path.read_text(encoding="utf-8") if script_path is not None else ""
        self.calls.append(Call(list(argv), cwd, dict(env), timeout_s, shell, script))
        if self._scripted:
            return self._scripted.pop(0)
        return subprocess_runner(argv, cwd=cwd, env=env, timeout_s=timeout_s, shell=shell)


@dataclass
class Review:
    """What one ``review pr --dry-run`` printed."""

    payload: dict[str, object]
    stderr: list[str]
    exit_code: int


def review(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    head: str,
    pr: int = 7,
    evidence: Path | None = None,
) -> Review:
    """Run ``review pr <pr> --dry-run`` at ``head`` from the checkout."""
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(
        review_cmds, "run_review", functools.partial(run_review, forge=Forge(repo, pr, head))
    )
    exit_code = 0
    capsys.readouterr()
    try:
        review_cmds.review_pr(
            pr,
            dry_run=True,
            forge_name=None,
            spec=None,
            provider=None,
            keep_tree=False,
            force=False,
            dry_run_reason="dry run",
            generated_evidence=str(evidence) if evidence is not None else None,
        )
    except typer.Exit as exited:
        exit_code = exited.exit_code
    out, err = capsys.readouterr()
    return Review(json.loads(out), err.splitlines(), exit_code)


def verify(
    repo: Repo,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    head: str,
    out: Path,
    pr: int = 7,
    runner: RecordingRunner | None = None,
) -> tuple[dict[str, object], list[str], RecordingRunner]:
    """Run ``review generated --pr <pr> --head <head> --base main`` from the checkout."""
    recorder = runner or RecordingRunner()
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(generated_verify.SEAMS, "runner", recorder)

    def reexec(executable: str, argv: list[str], env: dict[str, str]) -> None:
        recorder.reexecs.append((executable, list(argv), dict(env)))
        for name in [name for name in os.environ if name not in env]:
            monkeypatch.delenv(name)
        review_cmds.review_generated(pr_number=pr, head=head, out=str(out), base="main")

    monkeypatch.setattr(generated_verify.SEAMS, "execve", reexec)
    git(repo.checkout, "fetch", "-q", "origin", f"+refs/pull/{pr}/head:refs/pull/{pr}/head")
    capsys.readouterr()
    review_cmds.review_generated(pr_number=pr, head=head, out=str(out), base="main")
    _, err = capsys.readouterr()
    return json.loads(out.read_text(encoding="utf-8")), err.splitlines(), recorder
