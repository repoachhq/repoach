"""Tests for the review-bot team orchestrator + persistence + gh client.

The reviewers themselves call NIM and need a network — they're not in
scope here.  We exercise:

* :func:`record_review` round-trip on a tmp SQLite db.
* :class:`GhCli` graceful failure path when ``gh`` is missing.
* End-to-end :meth:`ReviewTeamOrchestrator.review_pr` with the
  reviewers + GhCli stubbed.
"""

from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import create_engine, text

from ferova.review.gh_client import GhCli, GhResult
from ferova.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from ferova.review.persistence import init_schema, record_review
from ferova.review.reviewer import (
    BotRole,
    ReviewComment,
    ReviewerOutcome,
    ReviewVerdict,
)


def _outcome(
    role: BotRole,
    verdict: ReviewVerdict,
    *,
    comments: list[ReviewComment] | None = None,
) -> ReviewerOutcome:
    """Build a ReviewerOutcome for tests with sensible defaults."""
    return ReviewerOutcome(
        role=role,
        verdict=verdict,
        summary="ok",
        comments=comments or [],
        model_used="qwen/qwen3-next-80b-a3b-instruct",
        elapsed_s=0.42,
        tokens_used=128,
        raw_response="{}",
    )


# ---------- L4 persistence ----------


def test_record_review_round_trip(tmp_path: Path):
    db = tmp_path / "review.db"
    init_schema(db)

    outcome = _outcome(
        BotRole.SENTINEL,
        ReviewVerdict.REQUEST_CHANGES,
        comments=[
            ReviewComment(
                file="src/foo.py",
                line=12,
                severity="blocker",
                body="Hardcoded secret",
            ),
            ReviewComment(
                file="src/foo.py",
                line=20,
                severity="major",
                body="Missing input validation",
            ),
            ReviewComment(
                file="src/foo.py",
                line=30,
                severity="minor",
                body="Naming",
            ),
        ],
    )
    record_review(db, pr_number=42, outcome=outcome)

    engine = create_engine(f"sqlite:///{db}")
    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT * FROM pr_reviews")).mappings())
    assert len(rows) == 1
    row = rows[0]
    assert row["pr_number"] == 42
    assert row["role"] == "sentinel"
    assert row["verdict"] == "REQUEST_CHANGES"
    assert row["n_comments"] == 3
    assert row["n_blockers"] == 1
    assert row["n_majors"] == 1
    parsed = json.loads(row["comments_json"])
    assert {c["severity"] for c in parsed} == {"blocker", "major", "minor"}
    assert row["decision_pivot"] is not None
    assert "blocker" in row["decision_pivot"]
    assert "src/foo.py:12" in row["decision_pivot"]
    assert "Hardcoded secret" in row["decision_pivot"]


def test_init_schema_idempotent(tmp_path: Path):
    db = tmp_path / "review.db"
    init_schema(db)
    init_schema(db)
    init_schema(db)


# ---------- GhCli graceful degradation ----------


def test_gh_cli_unavailable_returns_127(monkeypatch, tmp_path: Path):
    monkeypatch.setattr("shutil.which", lambda _name: None)
    cli = GhCli(cwd=tmp_path)
    assert not cli.available
    res = cli.pr_diff(99)
    assert isinstance(res, GhResult)
    assert res.returncode == 127
    assert "gh CLI not installed" in res.stderr
    assert not res.ok


class _ScriptedGhCli(GhCli):
    """GhCli whose gh / git subprocesses are replaced by canned results.

    ``gh_script`` and ``git_script`` map the first matching argv
    prefix (space-joined) to a ``(returncode, stdout, stderr)``
    triple; unmatched calls fail the test loudly so a scenario never
    silently exercises an unexpected command.  Every call is recorded
    in ``calls`` for ordering assertions.
    """

    def __init__(
        self,
        *,
        gh_script: dict[str, tuple[int, str, str]],
        git_script: dict[str, tuple[int, str, str]],
    ) -> None:
        super().__init__(cwd=Path("/tmp"), gh_path="/bin/true")
        self._gh_script = gh_script
        self._git_script = git_script
        self.calls: list[list[str]] = []

    def _replay(
        self,
        script: dict[str, tuple[int, str, str]],
        prog: str,
        args: list[str],
    ) -> GhResult:
        self.calls.append([prog, *args])
        joined = " ".join(args)
        for prefix, (rc, out, err) in script.items():
            if joined.startswith(prefix):
                return GhResult(returncode=rc, stdout=out, stderr=err, argv=[prog, *args])
        raise AssertionError(f"unexpected {prog} call: {joined}")

    def _run(self, args: list[str]) -> GhResult:
        return self._replay(self._gh_script, "gh", args)

    def _run_git(self, args: list[str]) -> GhResult:
        return self._replay(self._git_script, "git", args)


_PR_VIEW_JSON = json.dumps({"baseRefName": "develop", "headRefOid": "a" * 40})
_DIFF_406 = (1, "", "HTTP 406: Sorry, the diff exceeded the maximum number of files (300).")


def test_pr_diff_api_success_skips_fallback():
    cli = _ScriptedGhCli(
        gh_script={"pr diff 309": (0, "diff --git a/x b/x", "")},
        git_script={},
    )
    res = cli.pr_diff(309)
    assert res.ok
    assert res.stdout == "diff --git a/x b/x"
    assert all(call[0] == "gh" for call in cli.calls)


def test_pr_diff_falls_back_to_local_git_on_406():
    cli = _ScriptedGhCli(
        gh_script={
            "pr diff 309": _DIFF_406,
            "pr view 309": (0, _PR_VIEW_JSON, ""),
        },
        git_script={
            "fetch --quiet origin +refs/heads/develop": (0, "", ""),
            "cat-file -e": (0, "", ""),
            "diff origin/develop...": (0, "diff --git a/huge b/huge", ""),
        },
    )
    res = cli.pr_diff(309)
    assert res.ok
    assert res.stdout == "diff --git a/huge b/huge"


def test_pr_diff_fallback_fetches_pull_head_when_commit_missing():
    probes = iter([(1, "", "missing"), (0, "", "")])
    cli = _ScriptedGhCli(
        gh_script={
            "pr diff 309": _DIFF_406,
            "pr view 309": (0, _PR_VIEW_JSON, ""),
        },
        git_script={
            "fetch --quiet origin +refs/heads/develop": (0, "", ""),
            "fetch --quiet origin refs/pull/309/head": (0, "", ""),
            "diff origin/develop...": (0, "fallback diff", ""),
        },
    )
    original = cli._replay

    def replay_with_probe_sequence(script, prog, args):
        if prog == "git" and args[:2] == ["cat-file", "-e"]:
            cli.calls.append([prog, *args])
            rc, out, err = next(probes)
            return GhResult(returncode=rc, stdout=out, stderr=err, argv=[prog, *args])
        return original(script, prog, args)

    cli._replay = replay_with_probe_sequence
    res = cli.pr_diff(309)
    assert res.ok
    assert res.stdout == "fallback diff"
    assert ["git", "fetch", "--quiet", "origin", "refs/pull/309/head"] in cli.calls


def test_pr_diff_fallback_returns_api_error_when_view_fails():
    cli = _ScriptedGhCli(
        gh_script={
            "pr diff 309": _DIFF_406,
            "pr view 309": (1, "", "boom"),
        },
        git_script={},
    )
    res = cli.pr_diff(309)
    assert not res.ok
    assert "maximum number of files" in res.stderr


def test_pr_diff_fallback_returns_api_error_when_git_diff_fails():
    cli = _ScriptedGhCli(
        gh_script={
            "pr diff 309": _DIFF_406,
            "pr view 309": (0, _PR_VIEW_JSON, ""),
        },
        git_script={
            "fetch --quiet origin +refs/heads/develop": (0, "", ""),
            "cat-file -e": (0, "", ""),
            "diff origin/develop...": (128, "", "fatal: bad revision"),
        },
    )
    res = cli.pr_diff(309)
    assert not res.ok
    assert "maximum number of files" in res.stderr


# ---------- End-to-end orchestrator with stubs ----------


class _StubReviewer:
    """Reviewer drop-in that returns a canned ReviewerOutcome."""

    def __init__(self, outcome: ReviewerOutcome) -> None:
        self._outcome = outcome
        # Mirror Reviewer.role so the orchestrator can fetch
        # role-scoped resolved disagreements (SP-CODER-EVIDENCE-
        # CHALLENGE 3B).  ``role`` lives on the dataclass itself,
        # not the original Reviewer instance, which is fine for the
        # orchestrator's per-reviewer fetch loop.
        self.role = outcome.role

    def review_diff(
        self,
        diff: str,
        *,
        pr_number: int | None = None,
        spec_plan: str | None = None,
        resolved_disagreements: tuple[object, ...] | None = None,
        prior_review: object | None = None,
        extra_prompt_section: str = "",
        arch_edges: str = "",
    ) -> ReviewerOutcome:
        return self._outcome


class _StubGhCli:
    """Capture publish calls without hitting the network."""

    ARCHIVE_MARKER = "<!-- ferova-review-archive -->"

    def __init__(
        self,
        *,
        diff_ok: bool = True,
        review_submit_fail: bool = False,
    ) -> None:
        self._diff_ok = diff_ok
        self.review_submit_fail = review_submit_fail
        self.posted_comments: list[dict] = []
        self.posted_reviews: list[dict] = []
        self.archive_calls: list[dict] = []

    def pr_diff(self, pr_number: int) -> GhResult:
        if not self._diff_ok:
            return GhResult(1, "", "boom", argv=["gh", "pr", "diff"])
        return GhResult(
            0,
            (
                "diff --git a/a.py b/a.py\n"
                "--- a/a.py\n"
                "+++ b/a.py\n"
                "@@ -1 +1 @@\n"
                "+x = 1\n"
                "diff --git a/src/x.py b/src/x.py\n"
                "--- a/src/x.py\n"
                "+++ b/src/x.py\n"
                "@@ -1 +1 @@\n"
                "+y = 2\n"
            ),
            "",
            argv=["gh", "pr", "diff"],
        )

    def pr_head_sha(self, pr_number: int) -> str:
        return "deadbeef"

    def list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        # SP-CODER-EVIDENCE-CHALLENGE 3B: orchestrator pre-fetches
        # resolved disagreements per reviewer.  No fixture data here,
        # so return an empty list — the fetcher returns [] and the
        # reviewer prompts get the no-context stub.
        return []

    def pr_review_comment(self, pr_number: int, **kw) -> GhResult:
        self.posted_comments.append({"pr": pr_number, **kw})
        return GhResult(0, "", "", argv=["gh", "api"])

    def pr_review_submit(self, pr_number: int, **kw) -> GhResult:
        self.posted_reviews.append({"pr": pr_number, **kw})
        if self.review_submit_fail:
            return GhResult(
                1,
                "",
                "GraphQL: GITHUB_TOKEN cannot submit APPROVE",
                argv=["gh", "pr", "review"],
            )
        return GhResult(0, "", "", argv=["gh", "pr", "review"])

    def upsert_archive_comment(self, pr_number: int, *, body: str) -> GhResult:
        self.archive_calls.append({"pr": pr_number, "body": body})
        return GhResult(0, "", "", argv=["gh", "api"])

    def fetch_archive_comment(self, pr_number: int) -> str | None:
        # No prior archive in the stub — the orchestrator's preserve
        # path treats this as "nothing to carry over".
        return None


def test_review_pr_runs_team_and_publishes(tmp_path, monkeypatch):
    """All four reviewers run, verdict resolves, posts go through GhCli.

    Happy path: four APPROVE outcomes carrying only a minor comment →
    no blocking finding in the ledger and a complete review, so
    ``merge_gate.verdict_from_facts`` (SP-VERDICT-RESOURCE-LEDGER)
    yields ``final_verdict == APPROVE``. The blocking-finding and
    incomplete-review cases are covered in ``test_merge_gate.py``.
    """
    fake_outcomes = [
        _outcome(
            BotRole.ARCHITECT,
            ReviewVerdict.APPROVE,
            comments=[ReviewComment(file="a.py", line=1, severity="minor", body="nit")],
        ),
        _outcome(BotRole.SENTINEL, ReviewVerdict.APPROVE),
        _outcome(BotRole.TESTER, ReviewVerdict.APPROVE),
        _outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE),
    ]

    # Patch the four reviewer classes used inside the orchestrator so
    # they return our canned outcomes without touching NIM.
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(fake_outcomes[0]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(fake_outcomes[1]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(fake_outcomes[2]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(fake_outcomes[3]),
    )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=2,
    )
    team = orch.review_pr(pr_number=99)

    assert isinstance(team, TeamOutcome)
    assert team.pr_number == 99
    assert team.final_verdict == ReviewVerdict.APPROVE
    assert team.n_blockers == 0
    assert team.n_majors == 0
    assert len(team.reviews) == 4
    # 1 inline comment from Architect, 0 from the others.
    assert team.posted_comments == 1
    # Each of the 4 reviewers submits a final review.
    assert team.posted_reviews == 4
    assert len(gh.posted_reviews) == 4

    # L4 should hold one row per reviewer.
    engine = create_engine(f"sqlite:///{tmp_path / 'review.db'}")
    with engine.connect() as conn:
        rows = list(conn.execute(text("SELECT role FROM pr_reviews")).mappings())
    assert {r["role"] for r in rows} == {
        "architect",
        "sentinel",
        "tester",
        "scribe",
    }

    # SP-FINDER-OUTPUT dual-run: the Architect's one nit comment must
    # land in the findings ledger as a proposed advisory finding.
    from ferova.review.findings import fetch_findings

    findings = fetch_findings(tmp_path / "review.db", 99)
    assert len(findings) == 1
    assert findings[0].finder == "architect"
    assert findings[0].status.value == "proposed"


def test_findings_failure_never_breaks_review(monkeypatch, tmp_path: Path) -> None:
    """A findings-bridge crash is logged and the verdict flow survives."""
    fake_outcomes = [
        _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE),
        _outcome(BotRole.SENTINEL, ReviewVerdict.APPROVE),
        _outcome(BotRole.TESTER, ReviewVerdict.APPROVE),
        _outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE),
    ]
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(fake_outcomes[0]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(fake_outcomes[1]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(fake_outcomes[2]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(fake_outcomes[3]),
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("ledger unavailable")

    monkeypatch.setattr("ferova.review.orchestrator.record_findings_for_outcomes", _boom)

    orch = ReviewTeamOrchestrator(
        gh=_StubGhCli(),
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=2,
    )
    team = orch.review_pr(pr_number=99)
    assert isinstance(team, TeamOutcome)
    assert team.final_verdict == ReviewVerdict.APPROVE


def test_verify_failure_never_breaks_review(monkeypatch, tmp_path: Path) -> None:
    """A verifier crash is logged and the verdict flow survives (dual-run)."""
    fake = [_outcome(r, ReviewVerdict.APPROVE) for r in BotRole]
    by_role = {o.role: o for o in fake}
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(by_role[BotRole.ARCHITECT]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(by_role[BotRole.SENTINEL]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(by_role[BotRole.TESTER]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(by_role[BotRole.SCRIBE]),
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("verifier unavailable")

    monkeypatch.setattr("ferova.review.orchestrator.verify_findings_for_pr", _boom)

    orch = ReviewTeamOrchestrator(
        gh=_StubGhCli(), db_path=tmp_path / "review.db", post_to_github=True, max_workers=2
    )
    assert orch.review_pr(pr_number=99).final_verdict == ReviewVerdict.APPROVE


def test_judge_failure_never_breaks_review(monkeypatch, tmp_path: Path) -> None:
    """A refuter crash is logged and the verdict flow survives (dual-run)."""
    fake = [_outcome(r, ReviewVerdict.APPROVE) for r in BotRole]
    by_role = {o.role: o for o in fake}
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(by_role[BotRole.ARCHITECT]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(by_role[BotRole.SENTINEL]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(by_role[BotRole.TESTER]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(by_role[BotRole.SCRIBE]),
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("refuter unavailable")

    monkeypatch.setattr("ferova.review.orchestrator.judge_findings_for_pr", _boom)

    orch = ReviewTeamOrchestrator(
        gh=_StubGhCli(), db_path=tmp_path / "review.db", post_to_github=True, max_workers=2
    )
    assert orch.review_pr(pr_number=99).final_verdict == ReviewVerdict.APPROVE


def test_orchestrator_verifies_findings(monkeypatch, tmp_path: Path) -> None:
    """A Tester missing-test finding for an absent symbol is verified on disk."""
    tester = _outcome(
        BotRole.TESTER,
        ReviewVerdict.APPROVE,
        comments=[
            ReviewComment(
                file="src/x.py", line=1, severity="major", body="missing test_never_written_symbol"
            )
        ],
    )
    others = {
        BotRole.ARCHITECT: _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE),
        BotRole.SENTINEL: _outcome(BotRole.SENTINEL, ReviewVerdict.APPROVE),
        BotRole.SCRIBE: _outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE),
    }
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(others[BotRole.ARCHITECT]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(others[BotRole.SENTINEL]),
    )
    monkeypatch.setattr("ferova.review.orchestrator.Tester", lambda: _StubReviewer(tester))
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe", lambda: _StubReviewer(others[BotRole.SCRIBE])
    )

    orch = ReviewTeamOrchestrator(
        gh=_StubGhCli(),
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=2,
        repo_root=tmp_path,
    )
    orch.review_pr(pr_number=99)

    from ferova.review.findings import FindingStatus, fetch_findings

    verified = fetch_findings(tmp_path / "review.db", 99, status=FindingStatus.VERIFIED)
    assert len(verified) == 1
    assert verified[0].finder == "tester"


def test_review_pr_returns_safe_outcome_on_diff_fetch_failure(tmp_path, monkeypatch):
    """A failing ``gh pr diff`` short-circuits to a COMMENT verdict."""
    gh = _StubGhCli(diff_ok=False)
    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=False,
    )
    team = orch.review_pr(pr_number=7)
    assert team.final_verdict == ReviewVerdict.COMMENT
    assert team.posted_reviews == 0
    assert team.reviews == []


def test_review_pr_writes_archive_comment_when_posting(tmp_path, monkeypatch):
    """When publishing is enabled, a single archive comment is upserted.

    Uses an all-APPROVE outcome so the archive ``final_verdict`` is APPROVE
    (post SP-REVIEW-CONSENSUS-V2: any non-APPROVE verdict blocks consensus).
    """
    fake_outcomes = [
        _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE),
        _outcome(BotRole.SENTINEL, ReviewVerdict.APPROVE),
        _outcome(BotRole.TESTER, ReviewVerdict.APPROVE),
        _outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE),
    ]
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(fake_outcomes[0]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(fake_outcomes[1]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(fake_outcomes[2]),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(fake_outcomes[3]),
    )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=True)
    orch.review_pr(pr_number=42)

    assert len(gh.archive_calls) == 1
    body = gh.archive_calls[0]["body"]
    assert "ferova-review-archive" in body or "Full TeamOutcome" in body
    # The body includes a JSON fence with the team payload.
    assert "```json" in body
    assert '"pr_number": 42' in body
    assert '"final_verdict": "APPROVE"' in body


def test_review_pr_skips_archive_when_dry_run(tmp_path, monkeypatch):
    """Dry-run runs must not push any archive comment."""
    out = _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE)
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(out),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _StubReviewer(out),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(out),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(out),
    )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=False)
    orch.review_pr(pr_number=8)
    assert gh.archive_calls == []
    assert gh.posted_reviews == []


def test_review_submit_falls_back_to_issue_comment(tmp_path, monkeypatch):
    """When GitHub rejects APPROVE/REQUEST_CHANGES from a bot, post a comment instead."""
    out = _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE)
    for cls in ("Architect", "Sentinel", "Tester", "Scribe"):
        monkeypatch.setattr(
            f"ferova.review.orchestrator.{cls}",
            lambda o=out: _StubReviewer(o),
        )

    gh = _StubGhCli(review_submit_fail=True)
    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=True)
    team = orch.review_pr(pr_number=21)

    # Each of the 4 bots tried to submit a review (and failed)...
    assert len(gh.posted_reviews) == 4
    assert team.posted_reviews == 0
    # ...then posted a fallback issue comment that succeeded.
    assert team.posted_comments == 4
    fallback_bodies = [c["body"] for c in gh.posted_comments]
    assert all("verdict submission rejected" in b for b in fallback_bodies)


def test_one_failing_reviewer_does_not_break_team(tmp_path, monkeypatch):
    """A single reviewer crashing yields a degraded outcome, others run.

    SP-REVIEW-CONSENSUS-V2: a crashed reviewer's degraded outcome
    carries ``ReviewVerdict.COMMENT``, which now blocks consensus
    (previously it slipped through as APPROVE). The crash-resilience
    contract still holds — every other reviewer runs to completion —
    but the merge gate correctly refuses until the crashing bot
    re-runs cleanly. Same fixture, stronger assertion.
    """

    class _BoomReviewer:
        role = BotRole.SENTINEL

        def review_diff(self, diff, *, pr_number=None, **_kwargs):
            raise RuntimeError("synthetic NIM chain exhaustion")

    out_ok = _outcome(BotRole.ARCHITECT, ReviewVerdict.APPROVE)
    monkeypatch.setattr(
        "ferova.review.orchestrator.Architect",
        lambda: _StubReviewer(out_ok),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Sentinel",
        lambda: _BoomReviewer(),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Tester",
        lambda: _StubReviewer(_outcome(BotRole.TESTER, ReviewVerdict.APPROVE)),
    )
    monkeypatch.setattr(
        "ferova.review.orchestrator.Scribe",
        lambda: _StubReviewer(_outcome(BotRole.SCRIBE, ReviewVerdict.APPROVE)),
    )

    gh = _StubGhCli()
    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=False)
    team = orch.review_pr(pr_number=11)
    # Three healthy + one degraded sentinel = 4 outcomes.
    assert len(team.reviews) == 4
    sentinel_out = next(o for o in team.reviews if o.role == BotRole.SENTINEL)
    assert sentinel_out.verdict == ReviewVerdict.APPROVE
    assert "bot crashed" in sentinel_out.summary
    assert "[auto-promoted: nit-only]" in sentinel_out.summary
    assert team.final_verdict == ReviewVerdict.REQUEST_CHANGES, (
        "an unparsed (crashed) reviewer makes the review incomplete; the "
        "ledger-sourced verdict (SP-CODER-TRIGGER-FLIP) is REQUEST_CHANGES, "
        "in lockstep with the merge gate refusing on review_complete=False"
    )


def test_team_outcome_to_dict_shape():
    """The serialised payload exposes the per-bot comments in full."""
    from ferova.review.orchestrator import team_outcome_to_dict

    team = TeamOutcome(
        pr_number=99,
        final_verdict=ReviewVerdict.APPROVE,
        n_blockers=0,
        n_majors=1,
        reviews=[
            _outcome(
                BotRole.ARCHITECT,
                ReviewVerdict.COMMENT,
                comments=[ReviewComment(file="x.py", line=10, severity="major", body="extract")],
            )
        ],
        posted_comments=1,
        posted_reviews=1,
    )
    payload = team_outcome_to_dict(team)
    assert payload["pr_number"] == 99
    assert payload["final_verdict"] == "APPROVE"
    assert payload["reviews"][0]["role"] == "architect"
    assert payload["reviews"][0]["comments"][0]["severity"] == "major"
    # round-trips JSON cleanly
    assert json.loads(json.dumps(payload, default=str))["n_majors"] == 1


# ---------------------------------------------------------------------------
# SP-DEV-V2-B: the findings Coder is plan-aware (spec doc as fix context)
# ---------------------------------------------------------------------------


def test_respond_to_findings_substitutes_spec_plan_placeholder() -> None:
    """When spec_plan is provided, ``{SPEC_PLAN}`` is replaced in the prompt."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Coder

    loop = MagicMock()
    result = MagicMock()
    result.text = '{"fixes": [], "commit_message": "", "summary": "x"}'
    result.model_used = "stub"
    result.elapsed_s = 0.0
    result.tokens_used = 0
    loop.run_oneshot.return_value = result

    coder = Coder(loop=loop)
    coder.respond_to_findings(
        findings=[],
        diff="diff --git a/x b/x\n",
        spec_plan="# SP-DEV-V2-B unique-marker-zzz",
    )
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert "unique-marker-zzz" in rendered_prompt
    assert "{SPEC_PLAN}" not in rendered_prompt


def test_respond_to_findings_with_no_spec_plan_uses_placeholder() -> None:
    """When spec_plan is None, the prompt carries the fallback line."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Coder

    loop = MagicMock()
    result = MagicMock()
    result.text = '{"fixes": [], "commit_message": "", "summary": "x"}'
    result.model_used = "stub"
    result.elapsed_s = 0.0
    result.tokens_used = 0
    loop.run_oneshot.return_value = result

    coder = Coder(loop=loop)
    coder.respond_to_findings(findings=[], diff="diff")
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert "no spec context" in rendered_prompt
    assert "{SPEC_PLAN}" not in rendered_prompt


def test_reviewer_render_prompt_substitutes_spec_plan_placeholder() -> None:
    """When spec_plan is provided, every reviewer's rendered prompt carries it."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Architect, Scribe, Sentinel, Tester

    for cls in (Architect, Sentinel, Tester, Scribe):
        loop = MagicMock()
        result = MagicMock()
        result.text = '{"verdict": "APPROVE", "summary": "x", "comments": []}'
        result.model_used = "stub"
        result.elapsed_s = 0.0
        result.tokens_used = 0
        loop.run_oneshot.return_value = result
        reviewer = cls(loop=loop)
        reviewer.review_diff(
            "diff --git a/x b/x\n",
            spec_plan="# SP-DEV-V2-B2 reviewer-marker-yyy",
        )
        args, kwargs = loop.run_oneshot.call_args
        rendered_prompt = args[0] if args else kwargs.get("prompt", "")
        assert "reviewer-marker-yyy" in rendered_prompt, f"{cls.__name__}: marker missing"
        assert "{SPEC_PLAN}" not in rendered_prompt, f"{cls.__name__}: placeholder not substituted"


def test_reviewer_render_prompt_with_no_spec_plan_uses_placeholder() -> None:
    """When spec_plan is None, the prompt has the no-context fallback line."""
    from unittest.mock import MagicMock

    from ferova.review.reviewer import Architect

    loop = MagicMock()
    result = MagicMock()
    result.text = '{"verdict": "APPROVE", "summary": "x", "comments": []}'
    result.model_used = "stub"
    result.elapsed_s = 0.0
    result.tokens_used = 0
    loop.run_oneshot.return_value = result
    reviewer = Architect(loop=loop)
    reviewer.review_diff("diff --git a/x b/x\n")
    args, kwargs = loop.run_oneshot.call_args
    rendered_prompt = args[0] if args else kwargs.get("prompt", "")
    assert "no spec context" in rendered_prompt
    assert "{SPEC_PLAN}" not in rendered_prompt


def test_run_team_review_loads_spec_from_branch_name(tmp_path) -> None:
    """orchestrator.review_pr forwards the spec markdown to every reviewer."""
    from unittest.mock import MagicMock, patch

    from ferova.review.orchestrator import ReviewTeamOrchestrator

    gh = MagicMock()
    diff_res = MagicMock()
    diff_res.ok = True
    diff_res.stdout = "diff --git a/x b/x\n"
    gh.pr_diff.return_value = diff_res
    gh.pr_view.return_value = {"headRefName": "feat/sp-fake-impl"}
    gh.pr_head_sha.return_value = "abc123"
    gh.upsert_archive_comment.return_value = MagicMock(ok=True)
    gh.pr_review_submit.return_value = MagicMock(ok=True)
    gh.pr_review_comment.return_value = MagicMock(ok=True)

    fake_plan = MagicMock()
    fake_plan.raw_markdown = "# Fake spec — unique-spec-marker-aaa"

    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=False)
    # Patch AgentLoop in the reviewer module so Reviewer.__init__
    # does not try to validate the NIM API key (CI has no secret).
    with (
        patch("ferova.review.reviewer.AgentLoop", return_value=MagicMock()),
        patch("ferova.review.spec.maybe_load_active_spec", return_value=fake_plan) as mloader,
        patch("ferova.review.reviewer.Architect.review_diff") as march,
        patch("ferova.review.reviewer.Sentinel.review_diff") as msent,
        patch("ferova.review.reviewer.Tester.review_diff") as mtest,
        patch("ferova.review.reviewer.Scribe.review_diff") as mscribe,
    ):
        for m in (march, msent, mtest, mscribe):
            m.return_value = MagicMock(
                role=MagicMock(value="x"),
                verdict=MagicMock(value="APPROVE"),
                summary="ok",
                comments=[],
                model_used="stub",
                elapsed_s=0.0,
                tokens_used=0,
                raw_response="",
            )
        orch.review_pr(pr_number=999)

    mloader.assert_called_once()
    assert mloader.call_args.kwargs.get("branch") == "feat/sp-fake-impl"
    for m in (march, msent, mtest, mscribe):
        assert m.call_count == 1, "each reviewer dispatched exactly once"
        assert m.call_args.kwargs.get("spec_plan") == fake_plan.raw_markdown


def test_run_team_review_anchors_to_sub_specs_for_decomposed_parent(tmp_path) -> None:
    """A decomposed (superseded-parent) PR feeds reviewers the anchored sub-spec inputs."""
    from unittest.mock import MagicMock, patch

    from ferova.review.orchestrator import ReviewTeamOrchestrator
    from ferova.review.subspec_anchor import AnchoredReview

    gh = MagicMock()
    diff_res = MagicMock()
    diff_res.ok = True
    diff_res.stdout = "diff --git a/x b/x\n"
    gh.pr_diff.return_value = diff_res
    gh.pr_view.return_value = {"headRefName": "feat/sp-parent-impl"}
    gh.pr_head_sha.return_value = "abc123"
    gh.upsert_archive_comment.return_value = MagicMock(ok=True)
    gh.pr_review_submit.return_value = MagicMock(ok=True)
    gh.pr_review_comment.return_value = MagicMock(ok=True)

    anchored = AnchoredReview(
        parent_id="SP-PARENT",
        sub_spec_ids=("SP-PARENT-1", "SP-PARENT-2"),
        spec_plan_md="ANCHORED-MARKDOWN-marker-bbb",
        arch_edges="ANCHORED-EDGES-marker-ccc",
    )

    orch = ReviewTeamOrchestrator(gh=gh, db_path=tmp_path / "review.db", post_to_github=False)
    with (
        patch("ferova.review.reviewer.AgentLoop", return_value=MagicMock()),
        patch("ferova.review.spec.maybe_load_active_spec", return_value=None),
        patch(
            "ferova.review.subspec_anchor.maybe_anchor_decomposed_parent",
            return_value=anchored,
        ) as manchor,
        patch("ferova.review.reviewer.Architect.review_diff") as march,
        patch("ferova.review.reviewer.Sentinel.review_diff") as msent,
        patch("ferova.review.reviewer.Tester.review_diff") as mtest,
        patch("ferova.review.reviewer.Scribe.review_diff") as mscribe,
    ):
        for m in (march, msent, mtest, mscribe):
            m.return_value = MagicMock(
                role=MagicMock(value="x"),
                verdict=MagicMock(value="APPROVE"),
                summary="ok",
                comments=[],
                model_used="stub",
                elapsed_s=0.0,
                tokens_used=0,
                raw_response="",
            )
        orch.review_pr(pr_number=999)

    manchor.assert_called_once()
    assert manchor.call_args.args[0] == "feat/sp-parent-impl"
    for m in (march, msent, mtest, mscribe):
        assert m.call_args.kwargs.get("spec_plan") == "ANCHORED-MARKDOWN-marker-bbb"
        assert m.call_args.kwargs.get("arch_edges") == "ANCHORED-EDGES-marker-ccc"
