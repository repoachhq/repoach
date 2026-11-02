"""SP-OPEN-THREADS-BLOCK-THE-MERGE: no merge path merges a pull request with an open review thread.

The facts carry the count read from the forge and whether it was read;
the pure gate refuses on one open thread and on an unreadable listing.
The auto-merge of a green PR, the stranded sweep, the release gate and
the merge-on-ci trigger are each driven through their real code.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from typer.testing import CliRunner

from repoach.cli import review_cmds
from repoach.forge import ForgeError, ReviewThread
from repoach.review.auto_merge import OUTCOME_MERGED, OUTCOME_SKIP_GATE, run_auto_merge
from repoach.review.findings import init_findings_schema
from repoach.review.merge_gate import MergeFacts, compute_merge_decision
from repoach.review.merge_wait import StrandedPR
from repoach.review.open_threads import (
    OpenThreads,
    hold_open_threads,
    read_open_threads,
)
from repoach.review.release_gate import ReleaseFacts, compute_release_decision
from repoach.review.report import render_ledger_report
from tests.forge_fakes import thread_node
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

REPO_ROOT = Path(__file__).resolve().parents[2]
MERGE_ON_CI = REPO_ROOT / ".github" / "workflows" / "merge-on-ci.yml"
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
        "threads_read": True,
    }
    return MergeFacts(**{**base, **update})


def test_one_open_thread_refuses_and_zero_permits() -> None:
    """The gate names the count; with zero open threads the same facts merge."""
    assert compute_merge_decision(_facts()).merge is True
    refused = compute_merge_decision(_facts(unresolved_threads=1))
    assert refused.merge is False
    assert refused.reasons == ["1 unresolved review thread(s)"]
    assert compute_merge_decision(_facts(unresolved_threads=4)).reasons == [
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
    failed = compute_merge_decision(_facts(threads_read=False, threads_error="HTTP 502"))
    assert failed.merge is False
    assert failed.reasons == ["review threads unreadable: HTTP 502"]
    with pytest.raises(ValueError):
        MergeFacts(**{**_facts().model_dump(), "open_threads": 1})


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
    assert read.facts_update() == {
        "threads_read": True,
        "unresolved_threads": 2,
        "threads_error": "",
    }
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
    assert compute_merge_decision(_facts(**failed.facts_update())).merge is False


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
    """Open and unreadable threads hold a row; a PR with none is dispatched."""
    rows = [
        StrandedPR(pr_number=n, head_sha=str(n) * 40, green_since=NOW, reason="stranded")
        for n in (1, 2, 3)
    ]
    reads = {
        1: OpenThreads(read=True),
        2: OpenThreads(read=True, threads=[ReviewThread(root_id=5)]),
        3: OpenThreads(read=False, error="HTTP 403"),
    }
    dispatchable, held = hold_open_threads(rows, reads.__getitem__)
    assert [row.pr_number for row in dispatchable] == [1]
    assert [(h.pr_number, h.reason) for h in held] == [
        (2, "1 unresolved review thread(s)"),
        (3, "review threads unreadable: HTTP 403"),
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
    facts = _facts(unresolved_threads=3)
    body = render_ledger_report(
        tmp_path / "review.db",
        pr_number=7,
        decision=compute_merge_decision(facts),
        facts=facts,
    )
    assert "| Unresolved review threads | 3 |" in body
    assert "3 unresolved review thread(s)" in body
    unread = _facts(threads_read=False, threads_error="HTTP 502")
    body = render_ledger_report(
        tmp_path / "review.db", pr_number=7, decision=compute_merge_decision(unread), facts=unread
    )
    assert "| Unresolved review threads | unreadable |" in body


def _thread_event(
    *, actor: str = "jwfaye", draft: bool = False, base: str = "develop", repo: str = REPOSITORY
) -> Context:
    return Context(
        github={
            "event_name": "pull_request_review_thread",
            "actor": actor,
            "repository": REPOSITORY,
            "event": {
                "action": "resolved",
                "pull_request": {
                    "number": 259,
                    "draft": draft,
                    "base": {"ref": base},
                    "head": {"ref": "feat/x", "repo": {"full_name": repo}},
                },
            },
        }
    )


def test_merge_on_ci_rearms_on_a_resolved_thread() -> None:
    """The thread event triggers merge-on-ci for an allowed actor on a same-repo PR into develop."""
    workflow = yaml.safe_load(MERGE_ON_CI.read_text(encoding="utf-8"))
    assert workflow[True]["pull_request_review_thread"] == {"types": ["resolved", "unresolved"]}
    condition = workflow["jobs"]["merge"]["if"]
    assert job_condition(condition, _thread_event())
    assert not job_condition(condition, _thread_event(draft=True))
    assert not job_condition(condition, _thread_event(base="main"))
    assert not job_condition(condition, _thread_event(repo="fork/repoach"))
    assert not job_condition(condition, _thread_event(actor="someone-else"))
    group = workflow["concurrency"]["group"]
    assert "github.event.pull_request.head.ref" in group
    resolve = next(s for s in workflow["jobs"]["merge"]["steps"] if s.get("id") == "resolve")
    assert resolve["env"]["DISPATCH_PR"] == "${{ inputs.pr || github.event.pull_request.number }}"
