"""SP-OPEN-THREADS-BLOCK-THE-MERGE: no merge path merges a pull request with an open review thread.

The facts carry the count read from the forge and whether it was read;
the pure gate refuses on one open thread and on an unreadable listing.
The auto-merge of a green PR, the stranded sweep, the release gate and
the merge-on-ci trigger are each driven through their real code.
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
import yaml
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.forge import ForgeError, PullRequest, ReviewThread
from repoach.forge.github import GhResult
from repoach.forge.runner import CommandResult
from repoach.review import auto_merge
from repoach.review.auto_merge import (
    OUTCOME_MERGED,
    OUTCOME_SKIP_GATE,
    CIGateOutcome,
    GateEvaluation,
    evaluate_merge_gate,
    run_auto_merge,
)
from repoach.review.findings import init_findings_schema
from repoach.review.merge_gate import MergeFacts, compute_merge_decision
from repoach.review.merge_wait import StrandedPR
from repoach.review.open_threads import (
    OpenThreads,
    hold_open_threads,
    printable,
    read_open_threads,
)
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.release_gate import (
    ProvenanceSource,
    ReleaseFacts,
    compute_release_decision,
    gather_release_facts,
)
from repoach.review.report import render_ledger_report
from repoach.review.reviewer import BotRole, ReviewVerdict
from tests.forge_fakes import NO_OPEN_THREADS, thread_node, threads_page
from tests.github_concurrency_sim import Context, job_condition
from tests.merge_wait_fakes import (
    HEAD_256,
    HEAD_259,
    REPOSITORY,
    FixturePR,
    MergeWaitFixtureForge,
    ci_runs,
    measured_check_runs,
    seed_review,
)
from tests.unit.test_review_team import _outcome, _StubGitHubForge, _StubReviewer

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_ON_CI = REPO_ROOT / ".github" / "workflows" / "merge-on-ci.yml"
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
NOW = datetime.fromisoformat("2026-10-26T10:00:00+00:00")


def _facts(**update: object) -> MergeFacts:
    base: dict[str, object] = {
        "head_sha": "a" * 40,
        "ci_green": True,
        "open_blocking_findings": 0,
        "spec_covered": True,
        "spec_coverage_known": False,
        "review_complete": True,
        "review_integrity_known": True,
        "open_threads": OpenThreads(read=True),
    }
    return MergeFacts(**{**base, **update})


def _open(count: int) -> OpenThreads:
    return OpenThreads(read=True, threads=[ReviewThread(root_id=n + 1) for n in range(count)])


def test_one_open_thread_refuses_and_zero_permits() -> None:
    """The gate names the count; with zero open threads the same facts merge."""
    assert compute_merge_decision(_facts()).merge is True
    refused = compute_merge_decision(_facts(open_threads=_open(1)))
    assert refused.merge is False
    assert refused.reasons == ["1 unresolved review thread(s)"]
    assert compute_merge_decision(_facts(open_threads=_open(4))).reasons == [
        "4 unresolved review thread(s)"
    ]


def test_unread_threads_fail_closed() -> None:
    """Facts nobody read the threads for, or whose read failed, refuse with the reason."""
    unread = MergeFacts(
        head_sha="a" * 40,
        ci_green=True,
        open_blocking_findings=0,
        spec_covered=True,
        spec_coverage_known=False,
        review_complete=True,
        review_integrity_known=True,
    )
    assert compute_merge_decision(unread).reasons == ["review threads unreadable: not read"]
    failed = compute_merge_decision(_facts(open_threads=OpenThreads(read=False, error="HTTP 502")))
    assert failed.merge is False
    assert failed.reasons == ["review threads unreadable: HTTP 502"]
    with pytest.raises(ValueError):
        MergeFacts(**{**_facts().model_dump(), "unresolved_threads": 1})


class _Forge:
    """A forge whose ``unresolved_threads`` answer is scripted."""

    def __init__(self, answer: list[ReviewThread] | ForgeError) -> None:
        self.answer = answer

    def unresolved_threads(self, number: int) -> list[ReviewThread]:
        if isinstance(self.answer, ForgeError):
            raise self.answer
        return self.answer


def test_read_open_threads_names_each_thread_or_the_failure() -> None:
    """A read lists each thread's id and file:line; a failure is unread with its redacted reason."""
    threads = [
        ReviewThread(root_id=77, path="src/a.py", line=3),
        ReviewThread(root_id=9, thread_id="f" * 40),
    ]
    read = read_open_threads(_Forge(threads), 281)
    assert read.read is True and read.count == 2
    facts = _facts(open_threads=OpenThreads(read=False)).with_threads(read)
    assert (facts.threads_read, facts.unresolved_threads, facts.threads_error) == (True, 2, "")
    assert read.summary_line() == "review threads: 2 unresolved"
    assert read.thread_lines() == [
        "open thread 77 at src/a.py:3",
        f"open thread 9 ({'f' * 40}) at (no file)",
    ]
    assert read.as_json()["unresolved"] == 2
    assert read.as_json()["threads"][0] == {
        "root_id": 77,
        "thread_id": None,
        "path": "src/a.py",
        "line": 3,
    }

    failed = read_open_threads(
        _Forge(ForgeError("token ghp_abcdefghijklmnopqrstuvwxyz0123456789 HTTP 502")), 281
    )
    assert failed.read is False
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in failed.error
    assert failed.refusal() == f"review threads unreadable: {failed.error}"
    assert failed.summary_line() == f"review threads: unreadable: {failed.error}"
    assert failed.as_json()["unresolved"] is None
    assert compute_merge_decision(_facts().with_threads(failed)).merge is False


def _green(threads: list[dict[str, object]]) -> MergeWaitFixtureForge:
    return MergeWaitFixtureForge(
        [
            FixturePR(
                259,
                HEAD_259,
                "docs/sp-approved-spec-without-plan",
                check_runs=ci_runs(),
                threads=threads,
            )
        ]
    )


def test_auto_merge_refuses_a_green_pr_with_an_open_thread_then_merges_once_it_is_resolved(
    tmp_path: Path,
) -> None:
    """A green PR with an open thread is skipped and its attempts comment says why."""
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    open_thread = thread_node(4242, resolved=False, path="src/repoach/x.py", line=12)
    fx = _green([open_thread])

    first = run_auto_merge(
        259, forge=fx.forge, db_path=db, repo_root=REPO_ROOT, wait_seconds=0, clock=fx.clock
    )

    assert first.outcome == OUTCOME_SKIP_GATE
    assert first.notes == "pure gate refused: 1 unresolved review thread(s)"
    assert fx.merges == []
    comment = fx.attempts_comment()
    assert comment is not None
    assert "1 unresolved review thread(s)" in str(comment["body"])

    open_thread.update(isResolved=True, resolvedBy={"login": "jwfaye"})
    fx.advance(60)
    second = run_auto_merge(
        259, forge=fx.forge, db_path=db, repo_root=REPO_ROOT, wait_seconds=0, clock=fx.clock
    )
    assert second.outcome == OUTCOME_MERGED
    assert fx.merges == [259]


def test_auto_merge_refuses_when_the_threads_cannot_be_read(tmp_path: Path) -> None:
    """A failed thread read is a refusal naming the failure, never a merge."""
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    fx = _green([])
    answer = fx.forge._run

    def failing_graphql(args: list[str], *, input_data: str | None = None):
        if args[:2] == ["api", "graphql"]:
            return fx._fail(args, "gh: Something went wrong (HTTP 502)")
        return answer(args, input_data=input_data)

    fx.forge._run = failing_graphql
    result = run_auto_merge(
        259, forge=fx.forge, db_path=db, repo_root=REPO_ROOT, wait_seconds=0, clock=fx.clock
    )
    assert result.outcome == OUTCOME_SKIP_GATE
    assert "review threads unreadable: gh: Something went wrong (HTTP 502)" in result.notes
    assert fx.merges == []


def test_hold_open_threads_keeps_back_every_pr_its_threads_refuse() -> None:
    """Open and unreadable threads hold a row, the rest are dispatched; both keep row order."""
    rows = [
        StrandedPR(pr_number=n, head_sha=str(n) * 40, green_since=NOW, reason="stranded")
        for n in (4, 2, 3, 1, 5)
    ]
    reads = {
        4: OpenThreads(read=True),
        2: OpenThreads(read=True, threads=[ReviewThread(root_id=5)]),
        3: OpenThreads(read=False, error="HTTP 403"),
        1: OpenThreads(read=True),
        5: OpenThreads(read=False, error="HTTP 502"),
    }
    dispatchable, held = hold_open_threads(rows, reads.__getitem__)
    assert dispatchable == [rows[0], rows[3]]
    assert [(h.pr_number, h.head_sha, h.reason) for h in held] == [
        (2, "2" * 40, "1 unresolved review thread(s)"),
        (3, "3" * 40, "review threads unreadable: HTTP 403"),
        (5, "5" * 40, "review threads unreadable: HTTP 502"),
    ]


def test_stranded_dispatch_holds_a_green_pr_with_an_open_thread(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``review stranded --dispatch`` dispatches the clean PR and holds the one with a thread."""
    fx = MergeWaitFixtureForge(
        [
            FixturePR(
                256,
                HEAD_256,
                "fix/reverify-finds-the-test-by-name",
                check_runs=measured_check_runs(HEAD_256),
            ),
            FixturePR(
                259,
                HEAD_259,
                "docs/sp-approved-spec-without-plan",
                check_runs=measured_check_runs(HEAD_259),
                threads=[thread_node(31, resolved=False, path="a.py", line=1)],
            ),
        ],
        now=NOW,
    )
    monkeypatch.setattr(review_cmds, "select_forge", lambda **_k: fx.forge)
    monkeypatch.setattr(
        review_cmds,
        "get_settings",
        lambda: SimpleNamespace(db_path=str(tmp_path / "review.db"), integration_branch="develop"),
    )
    result = CliRunner().invoke(review_cmds.review_app, ["stranded", "--dispatch", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert fx.dispatches == ["workflow_run merge-on-ci.yml pr=256"]
    assert payload["dispatched"] == [256]
    assert payload["held"] == [
        {"pr_number": 259, "head_sha": HEAD_259, "reason": "1 unresolved review thread(s)"}
    ]
    assert "held #259 (1 unresolved review thread(s))" in result.stderr


def test_release_gate_refuses_open_or_unreadable_threads_on_the_release_pr() -> None:
    """The release PR's open threads refuse the release; no PR number reads none."""
    clean = {"develop_sha": "d" * 40, "remote_sha": "d" * 40, "ci_green": True}
    assert compute_release_decision(ReleaseFacts(**clean)).merge is True
    none = OpenThreads(read=True)
    two = OpenThreads(read=True, threads=[ReviewThread(root_id=1), ReviewThread(root_id=2)])
    unread = OpenThreads(read=False, error="HTTP 502")
    assert compute_release_decision(ReleaseFacts(**clean, open_threads=none)).merge is True
    assert compute_release_decision(ReleaseFacts(**clean, open_threads=two)).reasons == [
        "2 unresolved review thread(s)"
    ]
    assert compute_release_decision(ReleaseFacts(**clean, open_threads=unread)).reasons == [
        "review threads unreadable: HTTP 502"
    ]


def test_report_shows_the_thread_count_and_its_refusal(tmp_path: Path) -> None:
    """The archive report's facts table and headline carry the open threads."""
    init_findings_schema(tmp_path / "review.db")
    facts = _facts(open_threads=_open(3))
    body = render_ledger_report(
        tmp_path / "review.db",
        pr_number=7,
        decision=compute_merge_decision(facts),
        facts=facts,
    )
    assert "| Unresolved review threads | 3 |" in body
    assert "3 unresolved review thread(s)" in body
    unread = _facts(open_threads=OpenThreads(read=False, error="HTTP 502"))
    body = render_ledger_report(
        tmp_path / "review.db", pr_number=7, decision=compute_merge_decision(unread), facts=unread
    )
    assert "| Unresolved review threads | unreadable |" in body


ACTIONS_EVENTS = frozenset(
    {
        "branch_protection_rule",
        "check_run",
        "check_suite",
        "create",
        "delete",
        "deployment",
        "deployment_status",
        "discussion",
        "discussion_comment",
        "fork",
        "gollum",
        "issue_comment",
        "issues",
        "label",
        "merge_group",
        "milestone",
        "page_build",
        "public",
        "pull_request",
        "pull_request_review",
        "pull_request_review_comment",
        "pull_request_target",
        "push",
        "registry_package",
        "release",
        "repository_dispatch",
        "schedule",
        "status",
        "watch",
        "workflow_call",
        "workflow_dispatch",
        "workflow_run",
    }
)
"""The events a GitHub Actions workflow can listen to (docs: "Events that trigger workflows").

``pull_request_review_thread`` is a webhook event, not one of them: a
workflow listening to it failed as "a workflow file issue" on every push
(2026-11-03, runs 41550850883 to 41559980463).
"""


def test_every_workflow_listens_only_to_events_actions_offers() -> None:
    """No workflow waits on an event that can never start it."""
    for path in sorted(WORKFLOWS.glob("*.yml")):
        triggers = yaml.safe_load(path.read_text(encoding="utf-8"))[True]
        events = set(triggers) if isinstance(triggers, dict) else set(triggers or [])
        assert events <= ACTIONS_EVENTS, (path.name, sorted(events - ACTIONS_EVENTS))


def test_a_reply_in_a_thread_rearms_the_merge_through_the_review_workflow() -> None:
    """A reply is a submitted review: it re-runs auto-review, whose completion merge-on-ci admits."""
    review = yaml.safe_load((WORKFLOWS / "auto-review.yml").read_text(encoding="utf-8"))
    assert "submitted" in review[True]["pull_request_review"]["types"]
    merge = yaml.safe_load(MERGE_ON_CI.read_text(encoding="utf-8"))
    assert "Auto Review (review bots)" in merge[True]["workflow_run"]["workflows"]
    completed = Context(
        github={
            "event_name": "workflow_run",
            "actor": "jwfaye",
            "repository": REPOSITORY,
            "event": {
                "workflow_run": {
                    "conclusion": "success",
                    "event": "pull_request_review",
                    "head_repository": {"full_name": REPOSITORY},
                    "actor": {"login": "jwfaye"},
                }
            },
        }
    )
    assert job_condition(merge["jobs"]["merge"]["if"], completed)


def test_review_gate_names_every_thread_it_counted(monkeypatch: pytest.MonkeyPatch) -> None:
    """``review gate`` prints the count, one line per thread, and carries them in its JSON."""
    threads = OpenThreads(
        read=True,
        threads=[
            ReviewThread(root_id=4449188625, path="src/repoach/forge/github.py", line=396),
            ReviewThread(root_id=12, thread_id="e" * 40),
        ],
    )
    facts = _facts().with_threads(threads)
    evaluation = GateEvaluation(
        head_sha="a" * 40,
        ci_gate=CIGateOutcome(green=True, outcome_tag="APPROVE", reason=""),
        facts=facts,
        decision=compute_merge_decision(facts),
        pull_request=PullRequest(number=286, head_ref="feat/x", base_ref="develop", body=""),
        changed_files=[],
        open_threads=threads,
    )
    monkeypatch.chdir(REPO_ROOT)
    with patch("repoach.cli.review_cmds.evaluate_merge_gate", return_value=evaluation):
        result = CliRunner().invoke(review_cmds.review_app, ["gate", "286"])

    assert result.exit_code == 5, result.output
    payload = json.loads(result.stdout)
    assert payload["merge"] is False
    assert payload["reasons"] == ["2 unresolved review thread(s)"]
    assert payload["facts"]["unresolved_threads"] == 2
    assert payload["facts"]["threads_read"] is True
    assert payload["review_threads"]["threads"] == [
        {
            "root_id": 4449188625,
            "thread_id": None,
            "path": "src/repoach/forge/github.py",
            "line": 396,
        },
        {"root_id": 12, "thread_id": "e" * 40, "path": "", "line": None},
    ]
    stderr = result.stderr.splitlines()
    assert "review threads: 2 unresolved" in stderr
    assert "open thread 4449188625 at src/repoach/forge/github.py:396" in stderr
    assert f"open thread 12 ({'e' * 40}) at (no file)" in stderr


def _release_forge(threads: list[ReviewThread] | ForgeError) -> MagicMock:
    """A forge whose develop is clean and fresh, and whose release PR has ``threads``."""
    sha = "d" * 40
    forge = MagicMock()
    forge.head_sha.return_value = sha
    forge.merged_shas.return_value = {sha}

    def git(args: list[str]) -> CommandResult:
        stdout = ""
        if args[:2] == ["rev-parse", "develop"]:
            stdout = f"{sha}\n"
        elif args[:1] == ["log"]:
            stdout = f"{sha}\tfeat: x (#285)\n"
        elif args[:2] == ["ls-remote", "origin"]:
            stdout = f"{sha}\trefs/heads/develop\n"
        elif args[:2] == ["rev-list", "--count"]:
            stdout = "0\n"
        return CommandResult(returncode=0, stdout=stdout, stderr="", argv=args)

    forge.git.side_effect = git
    if isinstance(threads, ForgeError):
        forge.unresolved_threads.side_effect = threads
    else:
        forge.unresolved_threads.return_value = threads
    return forge


def _green_ci(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0)


def test_release_gate_reads_the_release_pr_threads_only_when_a_pr_is_given(
    tmp_path: Path,
) -> None:
    """``--pr`` reads the release PR's threads; without it nothing is read and nothing refuses."""
    open_one = _release_forge([ReviewThread(root_id=7, path="a.py", line=1)])
    facts = gather_release_facts(
        repo_root=tmp_path,
        forge=open_one,
        pr_number=290,
        ci_runner=_green_ci,
        provenance=ProvenanceSource.GITHUB,
    )
    assert facts.open_threads is not None and facts.open_threads.count == 1
    assert compute_release_decision(facts).reasons == ["1 unresolved review thread(s)"]
    open_one.unresolved_threads.assert_called_once_with(290)

    no_pr = _release_forge([ReviewThread(root_id=7)])
    facts = gather_release_facts(
        repo_root=tmp_path, forge=no_pr, ci_runner=_green_ci, provenance=ProvenanceSource.GITHUB
    )
    assert facts.open_threads is None
    assert compute_release_decision(facts).merge is True
    no_pr.unresolved_threads.assert_not_called()

    failing = _release_forge(ForgeError("gh: Something went wrong (HTTP 502)"))
    facts = gather_release_facts(
        repo_root=tmp_path,
        forge=failing,
        pr_number=290,
        ci_runner=_green_ci,
        provenance=ProvenanceSource.GITHUB,
    )
    assert compute_release_decision(facts).reasons == [
        "review threads unreadable: gh: Something went wrong (HTTP 502)"
    ]


class _ThreadReadsForge(_StubGitHubForge):
    """The review suite's posting forge whose thread reads answer ``answers`` in turn."""

    def __init__(self, answers: list[GhResult]) -> None:
        super().__init__()
        self.answers = answers
        self.thread_reads = 0

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:2] == ["api", "graphql"] and "isResolved path line" in args[-1]:
            self.thread_reads += 1
            return self.answers.pop(0)
        return super()._run(args, input_data=input_data)


def _approving_bench(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, role in (
        ("Architect", BotRole.ARCHITECT),
        ("Sentinel", BotRole.SENTINEL),
        ("Tester", BotRole.TESTER),
        ("Scribe", BotRole.SCRIBE),
    ):
        outcome = _outcome(role, ReviewVerdict.APPROVE)
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{name}", lambda _o=outcome, **_kw: _StubReviewer(_o)
        )


def test_review_pr_reads_the_threads_once_and_rereads_once_after_a_failed_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A posting run reads once before its archive; a failed read is read once more at the end."""
    _approving_bench(monkeypatch)
    one_open = json.dumps([threads_page([thread_node(5, resolved=False, path="a.py", line=1)])])
    healthy = _ThreadReadsForge([GhResult(0, one_open, "", argv=["gh"])])
    team = ReviewTeamOrchestrator(
        forge=healthy, db_path=tmp_path / "a.db", post_to_github=True
    ).review_pr(pr_number=42)
    assert healthy.thread_reads == 1
    assert team.open_threads.count == 1
    assert "| Unresolved review threads | 1 |" in healthy.archive_calls[0]["body"]

    flaky = _ThreadReadsForge(
        [
            GhResult(1, "", "gh: Something went wrong (HTTP 502)", argv=["gh"]),
            GhResult(0, NO_OPEN_THREADS, "", argv=["gh"]),
        ]
    )
    team = ReviewTeamOrchestrator(
        forge=flaky, db_path=tmp_path / "b.db", post_to_github=True
    ).review_pr(pr_number=42)
    assert flaky.thread_reads == 2
    assert "| Unresolved review threads | unreadable |" in flaky.archive_calls[0]["body"]
    assert team.open_threads.read is True
    assert team.open_threads.summary_line() == "review threads: 0 unresolved"


def test_an_unexpected_error_propagates_instead_of_holding_or_counting() -> None:
    """Only a ForgeError becomes an unread result; any other error fails loud through the sweep."""

    class _Broken:
        def unresolved_threads(self, number: int) -> list[ReviewThread]:
            raise RuntimeError("forge client bug")

    with pytest.raises(RuntimeError, match="forge client bug"):
        read_open_threads(_Broken(), 286)
    rows = [StrandedPR(pr_number=286, head_sha="a" * 40, green_since=NOW, reason="stranded")]
    with pytest.raises(RuntimeError, match="forge client bug"):
        hold_open_threads(rows, lambda number: read_open_threads(_Broken(), number))


@pytest.mark.parametrize(
    ("thread", "line"),
    [
        (ReviewThread(root_id=77, path="src/a.py", line=3), "open thread 77 at src/a.py:3"),
        (ReviewThread(root_id=77, path="src/a.py"), "open thread 77 at src/a.py"),
        (ReviewThread(root_id=0, path="src/a.py", line=3), "open thread ? at src/a.py:3"),
        (ReviewThread(root_id=9, thread_id="f" * 40), f"open thread 9 ({'f' * 40}) at (no file)"),
        (ReviewThread(root_id=0), "open thread ? at (no file)"),
    ],
)
def test_thread_lines_cover_missing_root_path_and_line(thread: ReviewThread, line: str) -> None:
    """A thread without a root id, a file or a line is still named, never dropped."""
    assert OpenThreads(read=True, threads=[thread]).thread_lines() == [line]


def test_the_gate_reads_the_threads_last_and_hands_them_to_review_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The thread read follows the ledger read, and ``review gate`` gets the threads it counted."""
    db = tmp_path / "review.db"
    seed_review(db, 259, HEAD_259)
    fx = _green([thread_node(31, resolved=False, path="src/x.py", line=4)])
    order: list[str] = []
    gather = auto_merge.gather_merge_facts

    def gather_logged(*args: object, **kwargs: object) -> MergeFacts:
        order.append("ledger")
        return gather(*args, **kwargs)

    answer = fx.forge._run

    def run_logged(args: list[str], *, input_data: str | None = None):
        if args[:2] == ["api", "graphql"]:
            order.append("threads")
        return answer(args, input_data=input_data)

    monkeypatch.setattr(auto_merge, "gather_merge_facts", gather_logged)
    fx.forge._run = run_logged
    evaluation = evaluate_merge_gate(
        259, forge=fx.forge, db_path=db, repo_root=REPO_ROOT, wait_seconds=0
    )
    assert order == ["ledger", "threads"]
    assert evaluation.decision.reasons == ["1 unresolved review thread(s)"]
    assert evaluation.open_threads is not None
    assert evaluation.open_threads.thread_lines() == ["open thread 31 at src/x.py:4"]


def test_a_file_name_or_error_from_the_forge_cannot_forge_a_line() -> None:
    """Control characters in a path or a forge error are escaped before ``review gate`` prints them."""
    spoof = ReviewThread(root_id=3, path="a.py\n::error::merged\x1b[2K", line=1)
    assert OpenThreads(read=True, threads=[spoof]).thread_lines() == [
        "open thread 3 at a.py\\n::error::merged\\x1b[2K:1"
    ]
    failed = read_open_threads(_Forge(ForgeError("HTTP 502\n::warning::spoofed")), 286)
    assert failed.error == "HTTP 502\\n::warning::spoofed"
    assert "\n" not in failed.summary_line()
    assert printable("tab\there") == "tab\\there"
    forged_id = ReviewThread(root_id=4, thread_id="ab\n::error::x")
    assert OpenThreads(read=True, threads=[forged_id]).thread_lines() == [
        "open thread 4 (ab\\n::error::x) at (no file)"
    ]
