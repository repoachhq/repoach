"""SP-FINDINGS-BRIDGE-DOCFIX 0.2-0.3: a re-review at the same head records nothing twice.

A real git origin carries ``refs/pull/7/head`` and a forge with a stubbed
``gh`` serves it. Two reviewers raise one finding each: a security
finding the judge confirms and a design finding the judge refutes. A
second ``review pr --dry-run`` at the same head replays the completed
run: no reviewer call, no judge call, ``cached: true``, the same
verdict. ``--force`` runs the reviewers again and the fingerprint keeps
the ledger free of duplicates; a review at a new head is not cached.
"""

from __future__ import annotations

import functools
import json
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path

import pytest
import typer

from repoach.ci_entry.advisory import CiReviewPlan, subprocess_review_runner
from repoach.ci_entry.variables import FORCE_VAR
from repoach.cli import review_cmds
from repoach.forge.github import GhResult, GitHubForge
from repoach.review import orchestrator
from repoach.review.diff_scoper import ScopedDiff
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from repoach.review.findings_bridge import (
    finding_fingerprint,
    record_findings_for_outcomes,
    record_oversized_findings,
)
from repoach.review.head_reverify import reverify_older_findings
from repoach.review.orchestrator import ReviewTeamOrchestrator, run_review
from repoach.review.refuter import judge_findings_for_pr
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict

_STORE = (
    "def search(db, prefix):\n"
    '    """Return the customers starting with prefix."""\n'
    "    return db.execute(f\"SELECT * FROM t WHERE c LIKE '{prefix}%'\")\n"
)
_DIFF = (
    "diff --git a/src/store.py b/src/store.py\n--- a/src/store.py\n+++ b/src/store.py\n"
    "@@ -1 +1 @@\n+x\n"
)
_SECURITY = "SQL injection: the prefix is interpolated into the query"
_DESIGN = "design: the query belongs in a repository class"


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
    """Refutes the design finding, confirms every other one, counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        refuted = "repository class" in prompt
        verdict = {"refuted": refuted, "reasoning": "a module function is fine" if refuted else ""}
        return f"VERDICT: {json.dumps(verdict)}"


class _StubReviewer:
    calls = 0

    def __init__(self, outcome: ReviewerOutcome) -> None:
        self._outcome = outcome
        self.role = outcome.role

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        _StubReviewer.calls += 1
        return self._outcome


def _outcome(role: BotRole, comments: list[ReviewComment]) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.REQUEST_CHANGES if comments else ReviewVerdict.APPROVE,
        summary="ok",
        comments=comments,
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="{}",
    )


def _db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "review.db"


@pytest.fixture
def judge(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _Judge:
    """Two reviewers raise one finding each; the refuter and the re-check share a judge."""
    outcomes = {
        "Architect": _outcome(
            BotRole.ARCHITECT,
            [ReviewComment(file="src/store.py", line=1, severity="major", body=_DESIGN)],
        ),
        "Sentinel": _outcome(
            BotRole.SENTINEL,
            [ReviewComment(file="src/store.py", line=3, severity="blocker", body=_SECURITY)],
        ),
        "Tester": _outcome(BotRole.TESTER, []),
        "Scribe": _outcome(BotRole.SCRIBE, []),
    }
    for name, outcome in outcomes.items():
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}",
            lambda _o=outcome, **_kw: _StubReviewer(_o),
        )
    monkeypatch.setattr("repoach.review.orchestrator.require_provider_ready", lambda: None)
    monkeypatch.setattr(_StubReviewer, "calls", 0)
    monkeypatch.setenv("REPOACH_DB_PATH", str(_db(tmp_path)))
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)
    monkeypatch.setattr("repoach.core.config._settings", None)
    stub = _Judge()
    monkeypatch.setattr(
        orchestrator,
        "judge_findings_for_pr",
        functools.partial(judge_findings_for_pr, judge_factory=lambda: stub),
    )
    monkeypatch.setattr(
        orchestrator,
        "reverify_older_findings",
        functools.partial(reverify_older_findings, judge_factory=lambda: stub),
    )
    return stub


def _dry_run(
    repo: _Repo,
    head: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    force: bool = False,
) -> tuple[dict[str, object], list[str]]:
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(
        review_cmds,
        "run_review",
        functools.partial(run_review, forge=_HeadForge(repo.checkout, head)),
    )
    with pytest.raises(typer.Exit) as exited:
        review_cmds.review_pr(
            7,
            dry_run=True,
            forge_name=None,
            spec=None,
            provider=None,
            keep_tree=False,
            force=force,
            dry_run_reason="dry run",
        )
    assert exited.value.exit_code == 2
    out, err = capsys.readouterr()
    prefixes = ("findings:", "review: cached")
    return json.loads(out), [line for line in err.splitlines() if line.startswith(prefixes)]


def _first_run(
    repo: _Repo,
    head: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    judge: _Judge,
) -> dict[str, object]:
    first, lines = _dry_run(repo, head, monkeypatch, capsys)
    assert lines == ["findings: 2 new, 0 already recorded, 0 refuted earlier"]
    assert first["findings_recorded"] == {"new": 2, "already_recorded": 0, "refuted_earlier": 0}
    assert first["cached"] is False
    assert judge.calls == 2
    assert _StubReviewer.calls > 0
    return first


def test_second_dry_run_at_the_same_head_replays_the_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    judge: _Judge,
) -> None:
    repo = _Repo(tmp_path)
    head = repo.push_head(_STORE, "plant")
    first = _first_run(repo, head, monkeypatch, capsys, judge)
    rows_after_first = fetch_findings(_db(tmp_path), 7)
    reviewer_calls = _StubReviewer.calls

    second, lines = _dry_run(repo, head, monkeypatch, capsys)

    assert _StubReviewer.calls == reviewer_calls
    assert judge.calls == 2
    assert second["cached"] is True
    assert second["final_verdict"] == first["final_verdict"] == "REQUEST_CHANGES"
    assert set(second) == set(first)
    assert second["reviews"] == first["reviews"]
    assert second["findings"] == first["findings"]
    [line] = lines
    assert line.startswith(f"review: cached at {head[:7]} (run {first['usage']['run_id']}, ")
    assert line.endswith("); use --force to re-review")
    assert fetch_findings(_db(tmp_path), 7) == rows_after_first


def test_force_runs_the_reviewers_and_records_nothing_twice(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    judge: _Judge,
) -> None:
    repo = _Repo(tmp_path)
    head = repo.push_head(_STORE, "plant")
    _first_run(repo, head, monkeypatch, capsys, judge)
    rows_after_first = fetch_findings(_db(tmp_path), 7)
    reviewer_calls = _StubReviewer.calls

    second, lines = _dry_run(repo, head, monkeypatch, capsys, force=True)
    rows_after_second = fetch_findings(_db(tmp_path), 7)

    assert _StubReviewer.calls > reviewer_calls
    assert second["cached"] is False
    assert lines == ["findings: 0 new, 1 already recorded, 1 refuted earlier"]
    assert second["findings_recorded"] == {"new": 0, "already_recorded": 1, "refuted_earlier": 1}
    assert len(rows_after_second) == 2
    assert judge.calls == 2
    statuses = {row.claim_type: row.status for row in rows_after_first}
    assert {row.claim_type: row.status for row in rows_after_second} == statuses
    assert statuses[ClaimType.DESIGN] is FindingStatus.REFUTED
    security_before = next(r for r in rows_after_first if r.claim_type is ClaimType.SECURITY)
    security_after = next(r for r in rows_after_second if r.claim_type is ClaimType.SECURITY)
    assert security_after.last_run_id
    assert security_after.last_run_id != security_before.last_run_id


def test_a_posting_run_at_a_cached_head_posts_the_stored_outcomes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    judge: _Judge,
) -> None:
    repo = _Repo(tmp_path)
    head = repo.push_head(_STORE, "plant")
    first = _first_run(repo, head, monkeypatch, capsys, judge)
    reviewer_calls = _StubReviewer.calls
    posted: list[list[ReviewerOutcome]] = []

    def _capture(self: ReviewTeamOrchestrator, **kwargs: object) -> None:
        posted.append(list(kwargs["outcomes"]))

    monkeypatch.setattr(ReviewTeamOrchestrator, "_publish_all", _capture)
    monkeypatch.chdir(repo.checkout)

    team = run_review(7, post=True, forge=_HeadForge(repo.checkout, head))

    assert team.cached is True
    assert _StubReviewer.calls == reviewer_calls
    assert judge.calls == 2
    [outcomes] = posted
    assert [o.role.value for o in outcomes] == [r["role"] for r in first["reviews"]]
    assert {c.body for o in outcomes for c in o.comments} == {_SECURITY, _DESIGN}


def test_a_new_head_records_its_findings_as_new(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    judge: _Judge,
) -> None:
    repo = _Repo(tmp_path)
    head_a = repo.push_head(_STORE, "plant")
    _dry_run(repo, head_a, monkeypatch, capsys)
    head_b = repo.push_head(_STORE + "\n\nLIMIT = 10\n", "second commit")

    second, lines = _dry_run(repo, head_b, monkeypatch, capsys)

    assert second["cached"] is False
    assert lines == ["findings: 2 new, 0 already recorded, 0 refuted earlier"]
    at_b = [row for row in fetch_findings(_db(tmp_path), 7) if row.head_sha == head_b]
    assert len(at_b) == 2


def _comment(body: str, line: int = 3) -> ReviewComment:
    return ReviewComment(file="src/store.py", line=line, severity="blocker", body=body)


def test_two_reviewers_raising_one_claim_record_one_row(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    outcomes = [
        _outcome(BotRole.SENTINEL, [_comment(_SECURITY)]),
        _outcome(
            BotRole.ARCHITECT,
            [_comment("  sql INJECTION:  the prefix is interpolated into the query! ", line=4)],
        ),
    ]

    recorded = record_findings_for_outcomes(
        db, pr_number=7, head_sha="a" * 40, outcomes=outcomes, round_n=1, diff=_DIFF
    )

    assert (recorded.new, recorded.already_recorded, recorded.refuted_earlier) == (1, 1, 0)
    assert len(fetch_findings(db, 7)) == 1


def test_fingerprint_ignores_the_reviewer_and_buckets_lines() -> None:
    base = dict(
        pr_number=7,
        head_sha="a",
        round=1,
        claim_type=ClaimType.SECURITY,
        severity=Severity.BLOCKING,
        file="src/store.py",
        line_end=3,
        evidence_pointer="x",
    )
    sentinel = Finding(finder="sentinel", line_start=3, claim=_SECURITY, **base)
    architect = Finding(finder="architect", line_start=4, claim=_SECURITY.upper(), **base)
    far = Finding(finder="sentinel", line_start=12, claim=_SECURITY, **base)

    assert finding_fingerprint(sentinel) == finding_fingerprint(architect)
    assert finding_fingerprint(sentinel) != finding_fingerprint(far)


def _refuted(db: Path, head: str, *, refuted_at: str) -> int:
    init_findings_schema(db)
    finding_id = record_finding(
        db,
        Finding(
            pr_number=7,
            head_sha=head,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SECURITY,
            severity=Severity.BLOCKING,
            file="src/store.py",
            line_start=3,
            line_end=3,
            claim=_SECURITY,
            evidence_pointer="src/store.py:3",
        ),
    )
    assert update_finding_status(
        db,
        finding_id,
        FindingStatus.REFUTED,
        verification_method="refuter",
        checked_at_sha=refuted_at,
    )
    return finding_id


def test_a_finding_refuted_at_the_head_is_never_reopened_there(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    head = "a" * 40
    finding_id = _refuted(db, head, refuted_at=head)

    recorded = record_findings_for_outcomes(
        db,
        pr_number=7,
        head_sha=head,
        outcomes=[_outcome(BotRole.SENTINEL, [_comment(_SECURITY)])],
        round_n=1,
        diff=_DIFF,
    )

    assert (recorded.new, recorded.refuted_earlier, recorded.reopened) == (0, 1, 0)
    [row] = fetch_findings(db, 7)
    assert row.id == finding_id
    assert row.status is FindingStatus.REFUTED


def test_a_finding_refuted_at_an_older_head_is_reopened_by_a_new_head(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    finding_id = _refuted(db, "a" * 40, refuted_at="a" * 40)

    recorded = record_findings_for_outcomes(
        db,
        pr_number=7,
        head_sha="b" * 40,
        outcomes=[_outcome(BotRole.SENTINEL, [_comment(_SECURITY)])],
        round_n=1,
        diff=_DIFF,
    )

    assert (recorded.new, recorded.refuted_earlier, recorded.reopened) == (1, 0, 1)
    reopened = next(row for row in fetch_findings(db, 7) if row.id == finding_id)
    assert reopened.status is FindingStatus.PROPOSED


def test_an_unknown_head_records_every_comment(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    outcomes = [_outcome(BotRole.SENTINEL, [_comment(_SECURITY)])]
    for _ in range(2):
        record_findings_for_outcomes(
            db, pr_number=7, head_sha=None, outcomes=outcomes, round_n=1, diff=_DIFF
        )

    assert len(fetch_findings(db, 7)) == 2


def test_an_oversized_file_is_recorded_once_per_head(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    scoped = ScopedDiff(prompt_diff="", included=[], omitted=[], oversized=["src/big.py"])

    counts = [
        record_oversized_findings(db, pr_number=7, head_sha="a" * 40, round_n=1, scoped=scoped)
        for _ in range(2)
    ]

    assert counts == [1, 0]
    assert len(fetch_findings(db, 7)) == 1


def test_a_ledger_from_before_the_last_seen_columns_migrates(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE pr_findings (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "pr_number INTEGER NOT NULL, head_sha VARCHAR NOT NULL, round INTEGER NOT NULL, "
            "finder VARCHAR NOT NULL, claim_type VARCHAR NOT NULL, severity VARCHAR NOT NULL, "
            "file VARCHAR NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, "
            "claim VARCHAR NOT NULL, evidence_pointer VARCHAR NOT NULL, status VARCHAR NOT NULL, "
            "verification_method VARCHAR NOT NULL, verification_result VARCHAR NOT NULL, "
            "checked_at_sha VARCHAR NOT NULL)"
        )
        conn.execute(
            "INSERT INTO pr_findings (pr_number, head_sha, round, finder, claim_type, severity, "
            "file, line_start, line_end, claim, evidence_pointer, status, verification_method, "
            "verification_result, checked_at_sha) VALUES (7, 'a', 1, 'sentinel', 'security', "
            "'blocking', 'src/store.py', 3, 3, 'x', 'y', 'proposed', '', '', '')"
        )

    init_findings_schema(db)

    [row] = fetch_findings(db, 7)
    assert (row.last_seen_at, row.last_run_id, row.verify_attempts) == ("", "", 0)


def test_ci_review_passes_force_only_when_asked(tmp_path: Path) -> None:
    script = tmp_path / "fakepython"
    script.write_text(
        f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    plan = CiReviewPlan(
        forge="gitlab",
        forge_source="GITLAB_CI",
        pr_number=306,
        pr_source="CI_MERGE_REQUEST_IID",
        mode="dry-run",
        provider_mode="gateway",
        forge_token_var="GITLAB_TOKEN",
    )
    runner = subprocess_review_runner(tmp_path, str(script))
    env = {"PATH": "/usr/bin:/bin"}

    _, plain = runner(plan, env)
    _, forced = runner(plan, {**env, FORCE_VAR: "1"})
    _, other = runner(plan, {**env, FORCE_VAR: "yes"})

    assert "--force" not in json.loads(plain)
    assert json.loads(forced)[-1] == "--force"
    assert "--force" not in json.loads(other)
