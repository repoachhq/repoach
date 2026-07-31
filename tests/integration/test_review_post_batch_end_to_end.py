"""Integration test: batched review submission through a real gh subprocess.

Exercises ``ReviewTeamOrchestrator.review_pr(pr_number=..., post_to_github=True)``
end to end (SP-REVIEW-POST-BATCH AC5) against a real scratch git repository under
``tmp_path`` and a real executable stand-in for ``gh`` — a Python script spawned as
a genuine subprocess that records its own argv + stdin to a log file, not a
Python-level stub — proving the orchestrator posts exactly one batched review per
reviewer instead of one legacy ``gh pr review`` call plus a per-comment burst.
"""

from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.gh_client import GhCli
from repoach.review.merge_gate import MergeFacts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.reviewer import (
    Architect,
    BotRole,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)

_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}

_LOG_ENV_VAR = "REPOACH_TEST_FAKE_GH_LOG"

_FAKE_GH_SCRIPT = """#!/usr/bin/env python3
import json
import os
import sys


def _record(argv, stdin_data):
    with open(os.environ["REPOACH_TEST_FAKE_GH_LOG"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"argv": argv, "stdin": stdin_data}) + "\\n")


def main() -> int:
    argv = sys.argv[1:]
    stdin_data = ""
    if "--input" in argv:
        stdin_data = sys.stdin.read()
    _record(argv, stdin_data)

    if argv[:2] == ["pr", "diff"]:
        sys.stdout.write(
            "diff --git a/a.py b/a.py\\n--- a/a.py\\n+++ b/a.py\\n@@ -1 +1 @@\\n+x = 1\\n"
        )
        return 0
    if argv[:2] == ["pr", "view"]:
        json.dump(
            {
                "number": 1,
                "title": "t",
                "body": "",
                "headRefName": "unrelated-branch",
                "baseRefName": "develop",
                "author": {"login": "x"},
                "state": "OPEN",
                "url": "http://x",
                "headRefOid": "deadbeefcafefeed",
            },
            sys.stdout,
        )
        return 0
    if argv and argv[0] == "api":
        method = None
        path = None
        rest = argv[1:]
        i = 0
        while i < len(rest):
            token = rest[i]
            if token == "--method":
                method = rest[i + 1]
                i += 2
                continue
            if token == "--input":
                i += 2
                continue
            if token in ("--paginate", "--slurp"):
                i += 1
                continue
            if token in ("-f", "-F"):
                i += 2
                continue
            if path is None:
                path = token
            i += 1
        if path is not None and path.endswith("/reviews") and method == "POST":
            json.dump({"id": 555}, sys.stdout)
            return 0
        if path is not None and "/comments" in path and method == "POST":
            json.dump({"id": 1}, sys.stdout)
            return 0
        if path is not None and "/comments" in path and method is None:
            sys.stdout.write("[[]]")
            return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _write_fake_gh(tmp_path: Path) -> Path:
    """Write the real gh stand-in script to *tmp_path* and make it executable."""
    script_path = tmp_path / "fake_gh.py"
    script_path.write_text(_FAKE_GH_SCRIPT, encoding="utf-8")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return script_path


def _canned(role: BotRole) -> ReviewerOutcome:
    """An all-APPROVE, zero-comment outcome for one role."""
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="canned",
        comments=[],
        model_used="test-model",
        elapsed_s=0.01,
        tokens_used=1,
        raw_response="{}",
    )


def _boundary_fake_reviewer(role: BotRole, outcome: ReviewerOutcome) -> Reviewer:
    """A real Architect/Sentinel/Tester/Scribe with only review_diff replaced.

    Mirrors ``tests/integration/test_fresh_head_concurrent.py``: the production
    reviewer class is instantiated for real (its ``AgentLoop`` boundary faked via
    a ``MagicMock`` that is never exercised, keeping the test hermetic), and only
    the public ``review_diff`` contract method is substituted.
    """
    instance = _ROLE_TO_CLASS[role](loop=MagicMock())
    instance.review_diff = lambda diff, **kwargs: outcome
    return instance


def _silence_agentmemory(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the review-lessons recall/remember loop (real-network calls).

    ``review_memory_enabled`` / ``review_lessons_enabled`` default to True
    (``src/repoach/core/config.py``), so left alone these would attempt a real
    HTTP call to the agentmemory service — not a gh call, but still a network
    dependency this integration test must not carry. Mirrors
    ``test_fresh_head_concurrent.py``'s own neutralisation of non-subject-under-
    test side effects.
    """
    monkeypatch.setattr(
        "repoach.review.orchestrator._build_review_lessons_block",
        lambda pr_title, diff: "",
    )
    monkeypatch.setattr(
        "repoach.review.orchestrator.remember_verified_findings",
        lambda *a, **kw: None,
    )


def _silence_ledger_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the ledger-facts summary so the archive body renders deterministically."""
    monkeypatch.setattr(
        "repoach.review.orchestrator.summarise_ledger_facts",
        lambda *a, **kw: MergeFacts(
            head_sha="deadbeefcafefeed",
            ci_green=True,
            open_blocking_findings=0,
            spec_covered=False,
            spec_coverage_known=False,
            review_complete=True,
            review_integrity_known=False,
            review_integrity_any=False,
        ),
    )


def test_review_pr_posts_batched_reviews_via_real_gh_subprocess(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC5: one batched ``gh api .../reviews`` call per reviewer, archive last.

    Drives ``review_pr`` end to end against a real scratch git repo and a real
    ``gh`` executable stand-in. Asserts exactly 4 ``gh api --method POST
    .../pulls/{pr}/reviews --input -`` calls (one per reviewer), zero legacy
    ``gh pr review`` calls, zero per-comment ``gh api --method POST
    .../pulls/{pr}/comments`` calls, and that the archive-comment upsert is the
    last call recorded in the log.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "feat/x", str(repo_root)], check=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=t@t",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "s",
        ],
        cwd=repo_root,
        check=True,
    )

    fake_gh = _write_fake_gh(tmp_path)
    log_path = tmp_path / "gh_calls.jsonl"
    monkeypatch.setenv(_LOG_ENV_VAR, str(log_path))

    gh = GhCli(cwd=repo_root, gh_path=str(fake_gh))

    for role, cls_name in (
        (BotRole.ARCHITECT, "Architect"),
        (BotRole.SENTINEL, "Sentinel"),
        (BotRole.TESTER, "Tester"),
        (BotRole.SCRIBE, "Scribe"),
    ):
        outcome = _canned(role)
        monkeypatch.setattr(
            f"repoach.review.orchestrator.{cls_name}",
            lambda db_path=None, role=role, outcome=outcome: _boundary_fake_reviewer(role, outcome),
        )

    _silence_agentmemory(monkeypatch)
    _silence_ledger_facts(monkeypatch)

    orch = ReviewTeamOrchestrator(
        gh=gh,
        db_path=tmp_path / "review.db",
        post_to_github=True,
        max_workers=4,
        repo_root=repo_root,
    )
    monkeypatch.setattr(orch, "_fire_routine", lambda _team: None)
    monkeypatch.setattr(orch, "_fire_proposed_escalation_dossier", lambda _pn: None)

    team = orch.review_pr(pr_number=3)

    assert team.final_verdict == ReviewVerdict.APPROVE
    assert team.posted_reviews == 4
    assert team.posted_comments == 0

    lines = log_path.read_text(encoding="utf-8").splitlines()
    calls = [json.loads(line) for line in lines]

    def _is_batched_review_post(call: dict) -> bool:
        argv = call["argv"]
        return (
            argv[:3] == ["api", "--method", "POST"]
            and argv[3].endswith("/pulls/3/reviews")
            and "--input" in argv
        )

    def _is_legacy_review_call(call: dict) -> bool:
        return call["argv"][:2] == ["pr", "review"]

    def _is_per_comment_post(call: dict) -> bool:
        argv = call["argv"]
        return argv[:3] == ["api", "--method", "POST"] and argv[3].endswith("/pulls/3/comments")

    def _is_archive_upsert(call: dict) -> bool:
        argv = call["argv"]
        return (
            argv[:2] == ["api", "--method"]
            and argv[2] == "POST"
            and argv[3].endswith("/issues/3/comments")
        )

    batched_calls = [c for c in calls if _is_batched_review_post(c)]
    assert len(batched_calls) == 4, f"expected 4 batched review calls, got {calls}"
    assert [c for c in calls if _is_legacy_review_call(c)] == []
    assert [c for c in calls if _is_per_comment_post(c)] == []

    for call in batched_calls:
        payload = json.loads(call["stdin"])
        assert payload["event"] == "APPROVE"
        assert payload["comments"] == []

    archive_calls = [c for c in calls if _is_archive_upsert(c)]
    assert len(archive_calls) == 1
    last_batch_index = max(calls.index(c) for c in batched_calls)
    archive_index = calls.index(archive_calls[0])
    assert archive_index > last_batch_index, (
        "archive-comment upsert must be recorded strictly after every batched review call"
    )
