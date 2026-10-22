"""SP-DRY-RUN-NEVER-POSTS G2-G4: a dry run performs no forge write.

A real :class:`GitHubForge` runs on a fake process spawner that models a
PR carrying one bot-authored inline thread at ``src/x.py:3``; the ledger
holds a refuted ``design`` finding at the same place, so a posting run
replies on that thread. Every public write method is wrapped to append
to a log before it reaches the barrier. A dry run leaves that log, the
spawned writes and the barrier's refusals all empty; a step that writes
anyway is refused by the barrier, counted, and makes ``review pr`` exit 8.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repoach.ci_entry.advisory import (
    CiReviewPlan,
    dry_run_refusal,
    interpret_review,
    plan_ci_review,
    subprocess_review_runner,
)
from repoach.cli.review_cmds import EXIT_WRITE_REFUSED, review_app
from repoach.forge import ForgeWriteRefusedError, WriteRefusal
from repoach.forge.github import GitHubForge
from repoach.forge.protocol import is_write_argv
from repoach.forge.runner import CommandResult
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from repoach.review.orchestrator import ReviewTeamOrchestrator, TeamOutcome
from repoach.review.refuter import JudgingReport
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict
from repoach.review.thread_context import post_refuted_finding_sentinels

_BOT = "github-actions[bot]"
_HEAD = "a" * 40
_DIFF = "diff --git a/src/x.py b/src/x.py\n--- a/src/x.py\n+++ b/src/x.py\n@@ -1 +1,3 @@\n+x = 1\n"
_ROOT = {
    "id": 100,
    "in_reply_to_id": None,
    "path": "src/x.py",
    "line": 3,
    "body": "**[architect/major]** this design couples x to y",
    "user": {"login": _BOT},
}
_WRITE_METHODS = (
    "post_comment",
    "post_review",
    "reply",
    "upsert_marked_comment",
    "merge",
    "create_pull_request",
)


def _answer(args: list[str]) -> str:
    if is_write_argv(args):
        return json.dumps({"id": 1})
    if args[:2] == ["pr", "diff"]:
        return _DIFF
    if args[:2] == ["pr", "view"]:
        return json.dumps(
            {
                "number": 7,
                "title": "t",
                "body": "",
                "headRefName": "feat/x",
                "baseRefName": "develop",
                "author": {"login": "dev"},
                "state": "OPEN",
                "url": "https://github.com/o/r/pull/7",
                "headRefOid": _HEAD,
                "isDraft": False,
            }
        )
    if any(a.endswith("pulls/7/comments") for a in args):
        return json.dumps([[_ROOT]])
    if "--slurp" in args:
        return "[[]]"
    return "[]"


class _ThreadedPr(GitHubForge):
    """GitHub with PR 7 open and one inline thread; every write method logs its call."""

    def __init__(self, gh: Path) -> None:
        super().__init__(cwd=gh.parent, gh_path=str(gh), bot_login=_BOT)
        self.argvs: list[list[str]] = []
        self.write_log: list[str] = []
        for name in _WRITE_METHODS:
            setattr(self, name, self._logged(name, getattr(self, name)))

    def _logged(self, name: str, method: Any) -> Any:
        def call(*args: Any, **kwargs: Any) -> Any:
            self.write_log.append(name)
            return method(*args, **kwargs)

        return call

    def _spawn(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        self.argvs.append(list(argv))
        return CommandResult(returncode=0, stdout=_answer(argv[1:]), stderr="", argv=argv)

    def spawned_writes(self) -> list[list[str]]:
        return [argv for argv in self.argvs if is_write_argv(argv[1:])]


class _Approving:
    def __init__(self, role: BotRole) -> None:
        self.role = role

    def review_diff(self, diff: str, **_kw: object) -> ReviewerOutcome:
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


@pytest.fixture
def gh(tmp_path: Path) -> Path:
    binary = tmp_path / "gh"
    binary.write_text("")
    return binary


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "review.db"
    init_findings_schema(path)
    record_finding(
        path,
        Finding(
            pr_number=7,
            head_sha=_HEAD,
            round=1,
            finder="architect",
            claim_type=ClaimType.DESIGN,
            severity=Severity.BLOCKING,
            file="src/x.py",
            line_start=3,
            line_end=3,
            claim="this design couples x to y",
            evidence_pointer="src/x.py:3",
            status=FindingStatus.REFUTED,
            verification_method="symbol_search",
            verification_result="no coupling at head",
        ),
    )
    return path


@pytest.fixture(autouse=True)
def _stub_models(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for role, cls in (
        (BotRole.ARCHITECT, "Architect"),
        (BotRole.SENTINEL, "Sentinel"),
        (BotRole.TESTER, "Tester"),
        (BotRole.SCRIBE, "Scribe"),
    ):
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{cls}", lambda _r=role, **_kw: _Approving(_r)
        )
    monkeypatch.setattr("repoach.review.orchestrator.verify_findings_for_pr", lambda *a, **k: {})
    monkeypatch.setattr(
        "repoach.review.orchestrator.judge_findings_for_pr", lambda *a, **k: JudgingReport()
    )
    yield


def _orchestrator(forge: GitHubForge, db: Path, tmp_path: Path, *, post: bool) -> Any:
    return ReviewTeamOrchestrator(
        forge=forge,
        db_path=db,
        post_to_github=post,
        repo_root=tmp_path,
        head_sha=_HEAD,
        write_refusal=None if post else WriteRefusal.DRY_RUN,
    )


def test_dry_run_on_a_threaded_pr_writes_nothing(gh: Path, db: Path, tmp_path: Path) -> None:
    """AC1: refuted finding + matching bot thread, dry run → no write call, no refusal."""
    forge = _ThreadedPr(gh)

    team = _orchestrator(forge, db, tmp_path, post=False).review_pr(7)

    assert forge.write_log == []
    assert forge.spawned_writes() == []
    assert forge.refused_writes == []
    assert team.refused_writes == []
    assert team.write_refusal == "dry run"
    assert (team.posted_comments, team.posted_reviews) == (0, 0)


def test_the_same_fixture_posting_replies_on_the_thread(gh: Path, db: Path, tmp_path: Path) -> None:
    """The fixture is armed: a posting run replies on thread 100 through the open barrier."""
    forge = _ThreadedPr(gh)

    team = _orchestrator(forge, db, tmp_path, post=True).review_pr(7)

    assert "reply" in forge.write_log
    assert any("pulls/7/comments/100/replies" in " ".join(a) for a in forge.spawned_writes())
    assert team.refused_writes == []
    assert team.write_refusal is None


def test_a_step_that_writes_in_a_dry_run_is_refused_and_counted(
    gh: Path, db: Path, tmp_path: Path
) -> None:
    """A posting step wired into the dry-run path reaches the barrier, which sends nothing."""
    forge = _ThreadedPr(gh)
    orch = _orchestrator(forge, db, tmp_path, post=False)
    orch._post = True

    with pytest.raises(ForgeWriteRefusedError) as refused:
        orch.review_pr(7)

    assert refused.value.reason is WriteRefusal.DRY_RUN
    assert forge.write_log
    assert forge.spawned_writes() == []
    assert forge.refused_writes


def test_a_swallowing_step_still_shows_in_the_count(gh: Path, db: Path) -> None:
    """F7: the sentinel step swallows a failed reply; the barrier's count still sees it."""
    forge = _ThreadedPr(gh)
    forge.barrier.close(WriteRefusal.DRY_RUN)

    assert post_refuted_finding_sentinels(forge, db, 7) == 0
    assert forge.spawned_writes() == []
    assert forge.refused_writes == ["reply #7"]


def _cli_review(
    monkeypatch: pytest.MonkeyPatch, forge: GitHubForge, db: Path, tmp_path: Path
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    def run_review(pr_number: int, **kwargs: Any) -> TeamOutcome:
        calls.append(kwargs)
        return ReviewTeamOrchestrator(
            forge=forge,
            db_path=db,
            post_to_github=bool(kwargs["post"]),
            repo_root=tmp_path,
            head_sha=_HEAD,
            write_refusal=kwargs["write_refusal"],
        ).review_pr(pr_number)

    monkeypatch.setattr("repoach.cli.review_cmds.run_review", run_review)
    return calls


def test_cli_dry_run_reports_zero_refused_writes(
    monkeypatch: pytest.MonkeyPatch, gh: Path, db: Path, tmp_path: Path
) -> None:
    """AC4 on the fixture: ``refused_writes`` is 0 and the stderr line names ``dry run``."""
    forge = _ThreadedPr(gh)
    calls = _cli_review(monkeypatch, forge, db, tmp_path)

    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["refused_writes"] == 0
    assert payload["write_refusal"] == "dry run"
    assert "repoach: refused_writes: 0 (dry run)" in result.stderr.splitlines()
    assert calls[0]["post"] is False
    assert calls[0]["write_refusal"] is WriteRefusal.DRY_RUN
    assert forge.spawned_writes() == []


def test_cli_exits_8_when_a_write_reached_the_barrier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    team = TeamOutcome(
        pr_number=7,
        final_verdict=ReviewVerdict.APPROVE,
        n_blockers=0,
        n_majors=0,
        refused_writes=["reply #7"],
        write_refusal="dry run",
    )
    monkeypatch.setattr("repoach.cli.review_cmds.run_review", lambda *a, **k: team)

    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])

    assert result.exit_code == EXIT_WRITE_REFUSED == 8
    assert json.loads(result.stdout)["refused_writes"] == 1
    lines = result.stderr.splitlines()
    assert "repoach: refused_writes: 1 (dry run)" in lines
    assert "repoach: dry run attempted a forge write: reply #7" in lines


def test_cli_exits_8_when_the_refusal_escapes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run_review(*_a: object, **_k: object) -> TeamOutcome:
        raise ForgeWriteRefusedError("post_review #7", WriteRefusal.DRY_RUN)

    monkeypatch.setattr("repoach.cli.review_cmds.run_review", run_review)

    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])

    assert result.exit_code == 8
    assert "repoach: dry run attempted a forge write: post_review #7" in result.stderr
    assert result.stdout == ""


def test_cli_rejects_an_unknown_dry_run_reason() -> None:
    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run", "--dry-run-reason", "nope"])
    assert result.exit_code == 2
    assert "--dry-run-reason must be one of" in result.stderr


@pytest.mark.parametrize(
    ("env", "dry_run", "post", "expected"),
    [
        ({}, False, False, WriteRefusal.ADVISORY_POST_OFF),
        ({"REPOACH_ADVISORY_POST": "true"}, False, False, WriteRefusal.ADVISORY_POST_OFF),
        ({"REPOACH_ADVISORY_POST": "1"}, True, False, WriteRefusal.DRY_RUN),
        ({"REPOACH_ADVISORY_POST": "1"}, False, False, None),
        ({}, False, True, None),
    ],
)
def test_ci_names_why_the_review_writes_nothing(
    env: dict[str, str], dry_run: bool, post: bool, expected: WriteRefusal | None
) -> None:
    plan = plan_ci_review(
        {
            "GITLAB_CI": "true",
            "CI_MERGE_REQUEST_IID": "3",
            "GITLAB_TOKEN": "t",
            "REPOACH_LLM_PROXY_BASE_URL": "http://gw",
            "REPOACH_ANTHROPIC_AUTH_TOKEN": "s",
            **env,
        },
        post=post,
        dry_run=dry_run,
    )
    assert isinstance(plan, CiReviewPlan)
    assert plan.write_refusal is expected
    assert dry_run_refusal(plan.mode, dry_run=dry_run) is expected


def test_ci_runner_passes_the_reason_to_the_review(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    seen: list[list[str]] = []

    class _Popen:
        returncode = 0
        pid = 1

        def __init__(self, argv: list[str], **_kw: object) -> None:
            seen.append(argv)

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return "{}", ""

    monkeypatch.setattr(subprocess, "Popen", _Popen)
    plan = CiReviewPlan(
        forge="gitlab",
        forge_source="GITLAB_CI",
        pr_number=3,
        pr_source="CI_MERGE_REQUEST_IID",
        mode="dry-run",
        provider_mode="gateway",
        forge_token_var="GITLAB_TOKEN",
        write_refusal=WriteRefusal.ADVISORY_POST_OFF,
    )

    subprocess_review_runner(tmp_path, python="py")(plan, {})

    assert seen[0][-3:] == ["--dry-run", "--dry-run-reason", "REPOACH_ADVISORY_POST is not 1"]


def test_ci_report_carries_the_count_and_names_exit_8() -> None:
    plan = CiReviewPlan(
        forge="gitlab",
        forge_source="GITLAB_CI",
        pr_number=3,
        pr_source="CI_MERGE_REQUEST_IID",
        mode="dry-run",
        provider_mode="gateway",
        forge_token_var="GITLAB_TOKEN",
    )
    clean = interpret_review(
        plan, 0, json.dumps({"final_verdict": "APPROVE", "refused_writes": 0}), "gateway"
    )
    assert clean.refused_writes == 0
    refused = interpret_review(
        plan, 8, json.dumps({"final_verdict": "APPROVE", "refused_writes": 2}), "gateway"
    )
    assert refused.refused_writes == 2
    assert refused.outcome == "internal_error"
    assert "attempted a forge write" in refused.reason


@pytest.fixture
def routine(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, object]]:
    """Routine secrets set, ``fire_review_routine`` replaced by a recorder."""
    from types import SimpleNamespace

    from repoach.review.notifier import RoutineFireResult

    settings = SimpleNamespace(
        claude_code_routine_id="rid",
        claude_code_routine_token=SimpleNamespace(get_secret_value=lambda: "tok"),
    )
    monkeypatch.setattr("repoach.review.orchestrator.get_settings", lambda: settings)
    fired: list[dict[str, object]] = []

    def fire(*, routine_id: str, token: str, payload: dict[str, object]) -> RoutineFireResult:
        fired.append(payload)
        return RoutineFireResult(
            ok=True, status_code=200, session_id="s", session_url="u", error=None
        )

    monkeypatch.setattr("repoach.review.orchestrator.fire_review_routine", fire)
    return fired


def test_the_review_outcome_routine_fire_is_refused_in_a_dry_run(
    gh: Path, db: Path, tmp_path: Path, routine: list[dict[str, object]]
) -> None:
    """A REQUEST_CHANGES outcome reaching ``_fire_routine`` in a dry run is refused and counted."""
    forge = _ThreadedPr(gh)
    orch = _orchestrator(forge, db, tmp_path, post=False)
    team = TeamOutcome(
        pr_number=7, final_verdict=ReviewVerdict.REQUEST_CHANGES, n_blockers=1, n_majors=0
    )

    with pytest.raises(ForgeWriteRefusedError) as refused:
        orch._fire_routine(team)

    assert refused.value.head == "routine fire review_outcome"
    assert routine == []
    assert forge.refused_writes == ["routine fire review_outcome"]


def test_the_proposed_escalation_routine_fire_is_refused_in_a_dry_run(
    gh: Path, tmp_path: Path, routine: list[dict[str, object]]
) -> None:
    """An escalation dossier reaching its routine fire in a dry run is refused and counted."""
    from repoach.review.stuck import PROPOSED_ESCALATION_ATTEMPTS

    path = tmp_path / "escalation.db"
    init_findings_schema(path)
    record_finding(
        path,
        Finding(
            pr_number=7,
            head_sha=_HEAD,
            round=1,
            finder="architect",
            claim_type=ClaimType.DESIGN,
            severity=Severity.BLOCKING,
            file="src/x.py",
            line_start=3,
            line_end=3,
            claim="unescalated blocker",
            evidence_pointer="src/x.py:3",
            status=FindingStatus.PROPOSED,
            verify_attempts=PROPOSED_ESCALATION_ATTEMPTS,
        ),
    )
    forge = _ThreadedPr(gh)
    orch = _orchestrator(forge, path, tmp_path, post=False)

    with pytest.raises(ForgeWriteRefusedError) as refused:
        orch._fire_proposed_escalation_dossier(7)

    assert refused.value.head == "routine fire proposed_escalation"
    assert routine == []
    assert forge.refused_writes == ["routine fire proposed_escalation"]
