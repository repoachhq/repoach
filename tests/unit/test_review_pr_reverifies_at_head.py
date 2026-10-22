"""SP-GATE-REVERIFIES-AT-HEAD G7-G10: ``review pr`` re-checks findings of older heads.

A real git origin carries ``refs/pull/7/head``. Head A plants an
f-string SQL query and a blocking ``security`` finding is recorded
``verified`` at A, the way the refuter leaves it after the review of A.
A person pushes head B. ``review pr`` at B, with four reviewers that
find nothing new, must re-check the finding against B's tree before it
computes the team verdict: ``APPROVE`` when B fixed it, and
``REQUEST_CHANGES`` naming the carried finding when B did not.
"""

from __future__ import annotations

import functools
import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from repoach.cli import review_cmds
from repoach.forge.github import GhResult, GitHubForge
from repoach.review import auto_merge, orchestrator
from repoach.review.auto_merge import CIGateOutcome, evaluate_merge_gate
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.head_reverify import reverify_older_findings
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome, run_review
from repoach.review.refuter import JudgingReport
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict

_PLANTED = (
    "def search(db, prefix):\n"
    '    """Return the customers starting with prefix."""\n'
    "    return db.execute(f\"SELECT * FROM t WHERE c LIKE '{prefix}%'\")\n"
)
_FIXED = (
    "def search(db, prefix):\n"
    '    """Return the customers starting with prefix."""\n'
    '    return db.execute("SELECT * FROM t WHERE substr(c, 1, length(?)) = ?", (prefix, prefix))\n'
)
_STILL_PLANTED = _PLANTED + '\n\ndef count(db):\n    """Count rows."""\n    return 0\n'
_DIFF = (
    "diff --git a/src/store.py b/src/store.py\n--- a/src/store.py\n+++ b/src/store.py\n"
    "@@ -1 +1 @@\n+x\n"
)
_NO_CHANGE = {"resolved": 0, "still_open": 0, "needs_judgement": 0}


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


class _Repo:
    """A seed clone that pushes heads to ``refs/pull/7/head`` of a bare origin."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        (self.seed / "README.md").write_text("demo\n", encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", "base")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.checkout))

    def push_head(self, content: str, message: str) -> str:
        target = self.seed / "src" / "store.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", message)
        _git(self.seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/7/head")
        return _git(self.seed, "rev-parse", "HEAD")


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


class _Judge:
    """A refuter that refutes unless the evidence still interpolates the prefix."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        planted = "{prefix}" in prompt.split("<<<EVIDENCE", 1)[-1]
        verdict = {
            "refuted": not planted,
            "reasoning": "the prefix is still interpolated" if planted else "the prefix is bound",
        }
        return f"VERDICT: {json.dumps(verdict)}"


class _StubReviewer:
    def __init__(self, outcome: ReviewerOutcome) -> None:
        self._outcome = outcome
        self.role = outcome.role

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        return self._outcome


def _approve(role: BotRole) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="ok",
        comments=[],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="{}",
    )


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Judge:
    """Stub four approving reviewers and the provider check; route the re-check to a judge."""
    outcomes = {
        "Architect": _approve(BotRole.ARCHITECT),
        "Sentinel": _approve(BotRole.SENTINEL),
        "Tester": _approve(BotRole.TESTER),
        "Scribe": _approve(BotRole.SCRIBE),
    }
    for name, outcome in outcomes.items():
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _o=outcome, **_kw: _StubReviewer(_o),
        )
    monkeypatch.setattr("repoach.review.orchestrator.require_provider_ready", lambda: None)
    monkeypatch.setenv("REPOACH_DB_PATH", str(_db(tmp_path)))
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)
    monkeypatch.setattr("repoach.core.config._settings", None)
    stub = _Judge()
    monkeypatch.setattr(
        orchestrator,
        "reverify_older_findings",
        functools.partial(reverify_older_findings, judge_factory=lambda: stub),
    )
    return stub


def _db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "review.db"


def _record_security(
    db: Path,
    head: str,
    *,
    line: int = 3,
    claim: str = "SQL injection: the prefix is interpolated into the query",
) -> int:
    init_findings_schema(db)
    return record_finding(
        db,
        Finding(
            pr_number=7,
            head_sha=head,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SECURITY,
            severity=Severity.BLOCKING,
            file="src/store.py",
            line_start=line,
            line_end=line,
            claim=claim,
            evidence_pointer=f"src/store.py:{line}",
            status=FindingStatus.VERIFIED,
            verification_method="refuter",
            verification_result="confirmed at A",
            checked_at_sha=head,
        ),
    )


def _review(repo: _Repo, head: str, monkeypatch: pytest.MonkeyPatch) -> TeamOutcome:
    monkeypatch.chdir(repo.checkout)
    return run_review(7, post=False, forge=_HeadForge(repo.checkout, head))


def _planted_then(tmp_path: Path, content: str) -> tuple[_Repo, str, str, int]:
    repo = _Repo(tmp_path)
    head_a = repo.push_head(_PLANTED, "plant")
    finding_id = _record_security(_db(tmp_path), head_a)
    head_b = repo.push_head(content, "second commit")
    return repo, head_a, head_b, finding_id


def test_review_at_the_fix_head_approves(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo, _, head_b, finding_id = _planted_then(tmp_path, _FIXED)

    team = _review(repo, head_b, monkeypatch)

    finding = next(f for f in fetch_findings(_db(tmp_path), 7) if f.id == finding_id)
    assert finding.status is FindingStatus.RESOLVED_AT_HEAD
    assert finding.verification_result.startswith(f"resolved at {head_b[:7]}")
    assert team.final_verdict is ReviewVerdict.APPROVE
    assert team.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert team.reverify_reasons == []
    assert team.reverify_line == (
        f"review: re-verified 1 finding(s) of older heads at {head_b[:7]}: "
        "resolved 1, still open 0, needs judgement 0"
    )
    assert judge.calls == 1


def test_review_at_a_non_fix_head_names_the_carried_finding(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo, head_a, head_b, finding_id = _planted_then(tmp_path, _STILL_PLANTED)

    team = _review(repo, head_b, monkeypatch)

    assert team.final_verdict is ReviewVerdict.REQUEST_CHANGES
    assert team.reverified == {"resolved": 0, "still_open": 1, "needs_judgement": 0}
    [reason] = team.reverify_reasons
    assert reason.startswith(
        f"finding {finding_id}: security src/store.py:3 raised at {head_a[:7]}, "
        f"still present at {head_b[:7]}"
    )
    finding = next(f for f in fetch_findings(_db(tmp_path), 7) if f.id == finding_id)
    assert finding.status is FindingStatus.VERIFIED
    assert finding.checked_at_sha == head_b


def test_second_review_and_gate_at_the_same_head_make_no_model_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo, head_a, head_b, _ = _planted_then(tmp_path, _STILL_PLANTED)
    first = _review(repo, head_b, monkeypatch)
    assert judge.calls == 1

    second = _review(repo, head_b, monkeypatch)

    assert judge.calls == 1
    assert second.final_verdict is ReviewVerdict.REQUEST_CHANGES
    assert (
        second.reverified
        == first.reverified
        == {
            "resolved": 0,
            "still_open": 1,
            "needs_judgement": 0,
        }
    )
    assert len(second.reverify_reasons) == 1
    assert f"raised at {head_a[:7]}, still present at {head_b[:7]}" in second.reverify_reasons[0]

    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: None)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (head_b, ""))
    monkeypatch.setattr(
        auto_merge,
        "evaluate_ci_gate",
        lambda *_a, **_k: CIGateOutcome(green=True, outcome_tag="GREEN", reason=""),
    )
    evaluation = evaluate_merge_gate(
        7,
        forge=_HeadForge(repo.checkout, head_b),
        db_path=_db(tmp_path),
        repo_root=repo.checkout,
        pin_tree=True,
        judge_factory=lambda: judge,
    )

    assert judge.calls == 1
    assert evaluation.decision.merge is False
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 1, "needs_judgement": 0}
    carried = [r for r in evaluation.decision.reasons if "still present" in r]
    assert len(carried) == 1


def test_finding_of_the_reviewed_head_is_left_to_the_refuter_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo = _Repo(tmp_path)
    head = repo.push_head(_PLANTED, "plant")
    _record_security(_db(tmp_path), head)

    team = _review(repo, head, monkeypatch)

    assert judge.calls == 0
    assert team.reverified == _NO_CHANGE
    assert team.final_verdict is ReviewVerdict.REQUEST_CHANGES


def test_broken_behavior_of_an_older_head_is_left_to_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo, head_a, head_b, _ = _planted_then(tmp_path, _FIXED)
    record_finding(
        _db(tmp_path),
        Finding(
            pr_number=7,
            head_sha=head_a,
            round=1,
            finder="ci",
            claim_type=ClaimType.BROKEN_BEHAVIOR,
            severity=Severity.BLOCKING,
            file="src/store.py",
            line_start=1,
            line_end=1,
            claim="CI failed: tests",
            evidence_pointer="src/store.py:1",
            status=FindingStatus.VERIFIED,
            verification_method="ci",
            verification_result="red",
            checked_at_sha=head_a,
        ),
    )

    team = _review(repo, head_b, monkeypatch)

    assert team.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    statuses = {f.claim_type: f.status for f in fetch_findings(_db(tmp_path), 7)}
    assert statuses[ClaimType.BROKEN_BEHAVIOR] is FindingStatus.VERIFIED


def test_unjudgeable_finding_keeps_the_verdict_and_says_why(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    def _no_judge() -> Callable[[str], str]:
        raise RuntimeError("no provider")

    monkeypatch.setattr(
        orchestrator,
        "reverify_older_findings",
        functools.partial(reverify_older_findings, judge_factory=_no_judge),
    )
    repo, _, head_b, _ = _planted_then(tmp_path, _FIXED)

    team = _review(repo, head_b, monkeypatch)

    assert team.final_verdict is ReviewVerdict.REQUEST_CHANGES
    assert team.reverified == {"resolved": 0, "still_open": 0, "needs_judgement": 1}
    assert (
        "unresolved: needs judgement (judge call failed: RuntimeError)"
        in (team.reverify_reasons[0])
    )


def test_cli_prints_the_line_and_the_json_keys(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    judge: _Judge,
) -> None:
    repo, _, head_b, _ = _planted_then(tmp_path, _FIXED)
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(
        review_cmds,
        "run_review",
        functools.partial(run_review, forge=_HeadForge(repo.checkout, head_b)),
    )

    review_cmds.review_pr(
        7,
        dry_run=True,
        forge_name=None,
        spec=None,
        provider=None,
        keep_tree=False,
        dry_run_reason="dry run",
    )

    out, err = capsys.readouterr()
    payload = json.loads(out)
    assert payload["final_verdict"] == "APPROVE"
    assert payload["reverified"] == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert payload["reverify_reasons"] == []
    lines = [line for line in err.splitlines() if line.startswith("review: re-verified")]
    assert lines == [
        f"review: re-verified 1 finding(s) of older heads at {head_b[:7]}: "
        "resolved 1, still open 0, needs judgement 0"
    ]


def test_re_check_budget_is_the_cap_minus_the_refuter_pass_calls(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    monkeypatch.setenv("REPOACH_REFUTER_MAX_JUDGED", "2")
    monkeypatch.setattr(
        orchestrator, "judge_findings_for_pr", lambda *_a, **_k: JudgingReport(model_calls=1)
    )
    repo, head_a, head_b, _ = _planted_then(tmp_path, _FIXED)
    _record_security(_db(tmp_path), head_a, line=1, claim="search trusts its caller")

    team = _review(repo, head_b, monkeypatch)

    assert judge.calls == 1
    assert team.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 1}
    [deferred] = team.reverify_reasons
    assert deferred.endswith("unresolved: needs judgement (judging cap 2 reached)")


def test_unpinned_review_never_re_checks_against_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, judge: _Judge
) -> None:
    repo, _, head_b, finding_id = _planted_then(tmp_path, _FIXED)
    assert not (repo.checkout / "src" / "store.py").exists()
    monkeypatch.chdir(repo.checkout)
    orch = ReviewTeamOrchestrator(
        forge=_HeadForge(repo.checkout, head_b),
        db_path=_db(tmp_path),
        post_to_github=False,
        repo_root=repo.checkout,
    )
    monkeypatch.setattr(orch, "_join_head_guard", lambda *_a, **_k: head_b)

    team = orch.review_pr(7)

    finding = next(f for f in fetch_findings(_db(tmp_path), 7) if f.id == finding_id)
    assert finding.status is FindingStatus.VERIFIED
    assert finding.checked_at_sha != head_b
    assert judge.calls == 0
    assert (
        team.reverify_line
        == "review: findings of older heads not re-verified (no head tree pinned)"
    )
    assert team.reverify_reasons == [
        "findings of older heads not re-verified (no head tree pinned)"
    ]
    assert team.final_verdict is ReviewVerdict.REQUEST_CHANGES
