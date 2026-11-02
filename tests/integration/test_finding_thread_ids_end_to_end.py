"""SP-FINDING-THREAD-IDS C3: a posted finding's thread is recorded and a ``/resolve`` reply settles it by id.

A real bare origin carries ``refs/pull/7/head``. A posting ``review pr 7``
runs through the real :class:`~repoach.forge.github.GitHubForge` with a
stubbed ``gh`` that plays GitHub in memory: the Sentinel's blocking
comment goes out in one batched review, whose comment ids are listed
back, and the run records one ``finding_posts`` row for it. The forge
then reports that root comment at another line (the file moved under
it) and a maintainer's ``/resolve <reason>`` reply in its thread. The
next ``review pr --dry-run`` at the same head (a replay) applies the
command to the recorded finding by its thread id, where the legacy
``(file, line)`` match finds nothing, and ends APPROVE.
"""

from __future__ import annotations

import functools
import itertools
import json
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult
from repoach.review import orchestrator
from repoach.review.finding_posts import fetch_finding_posts
from repoach.review.findings import FindingStatus, fetch_findings
from repoach.review.orchestrator import run_review
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict
from tests.fixtures.generated_artefacts.harness import Forge, Repo, Review, install

PR = 7
REASON = "the branch protection is read back from the API and requires two approvals"


class GitHub(Forge):
    """The harness forge, playing GitHub's review, comment and permission endpoints in memory."""

    def __init__(self, repo: Repo, head: str) -> None:
        super().__init__(repo, PR, head)
        self.inline: list[dict[str, object]] = []
        self.issue: list[dict[str, object]] = []
        self._ids = itertools.count(4437504238)
        self.review_ids: list[int] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        endpoint = next((a for a in args[1:] if "/" in a and not a.startswith("-")), "")
        if args[:3] == ["api", "--method", "POST"] and endpoint.endswith(f"/pulls/{PR}/reviews"):
            payload = json.loads(input_data or "{}")
            review_id = next(self._ids)
            self.review_ids.append(review_id)
            for draft in payload.get("comments", []):
                self.inline.append(
                    {
                        "id": next(self._ids),
                        "pull_request_review_id": review_id,
                        "user": {"login": "bot"},
                        "path": draft["path"],
                        "line": draft["line"],
                        "body": draft["body"],
                        "created_at": "2026-11-02T10:00:00Z",
                        "updated_at": "2026-11-02T10:00:00Z",
                    }
                )
            return GhResult(0, json.dumps({"id": review_id}), "", argv=args)
        if "/reviews/" in endpoint and endpoint.endswith("/comments"):
            review_id = int(endpoint.split("/")[-2])
            listed = [c for c in self.inline if c.get("pull_request_review_id") == review_id]
            return GhResult(0, json.dumps(listed), "", argv=args)
        if endpoint.endswith(f"/pulls/{PR}/comments"):
            return GhResult(0, json.dumps([self.inline]), "", argv=args)
        if endpoint.endswith(f"/issues/{PR}/comments"):
            if "POST" in args:
                return GhResult(0, json.dumps({"id": next(self._ids)}), "", argv=args)
            return GhResult(0, json.dumps([self.issue]), "", argv=args)
        if args[:2] == ["api", "graphql"]:
            return GhResult(0, json.dumps([{"data": {"repository": None}}]), "", argv=args)
        if len(args) == 2 and args[0] == "api" and args[1].endswith("/permission"):
            role = "maintain" if args[1].split("/")[-2] == "jwfaye" else "read"
            return GhResult(0, json.dumps({"role_name": role}), "", argv=args)
        return super()._run(args, input_data=input_data)


class Blocking:
    """A Sentinel raising one blocking finding on ``src/settings.py`` line 2."""

    def __init__(self, **_kwargs: object) -> None:
        self.role = BotRole.SENTINEL

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        return ReviewerOutcome(
            role=BotRole.SENTINEL,
            verdict=ReviewVerdict.REQUEST_CHANGES,
            summary="one blocker",
            comments=[
                ReviewComment(
                    file="src/settings.py",
                    line=2,
                    severity="blocker",
                    body="main is not protected: two approvals are not required",
                )
            ],
            model_used="fake",
            elapsed_s=0.0,
            tokens_used=0,
            raw_response="{}",
        )


def _review(
    repo: Repo,
    forge: GitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    dry_run: bool,
) -> Review:
    """Run ``review pr 7`` from the checkout through ``forge``."""
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(review_cmds, "run_review", functools.partial(run_review, forge=forge))
    exit_code = 0
    capsys.readouterr()
    try:
        review_cmds.review_pr(
            PR,
            dry_run=dry_run,
            forge_name=None,
            spec=None,
            provider=None,
            keep_tree=False,
            force=False,
            dry_run_reason="dry run",
            generated_evidence=None,
        )
    except typer.Exit as exited:
        exit_code = exited.exit_code
    out, err = capsys.readouterr()
    assert out, err
    return Review(json.loads(out), err.splitlines(), exit_code)


def test_a_resolve_reply_settles_the_posted_finding_by_its_recorded_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Posted, recorded, then settled by thread id after its root line moved."""
    bench = install(monkeypatch, tmp_path)
    monkeypatch.setattr(orchestrator, "Sentinel", Blocking)
    monkeypatch.setattr(orchestrator, "fire_review_routine", lambda *_a, **_k: None)
    repo = Repo(tmp_path / "repo", {"README.md": "base\n"})
    head = repo.push_head({"src/settings.py": "PROTECTED = False\nAPPROVALS = 1\n"})
    forge = GitHub(repo, head)

    posted = _review(repo, forge, monkeypatch, capsys, dry_run=False)

    assert posted.payload["final_verdict"] == "REQUEST_CHANGES"
    assert posted.payload["finding_posts"] == 1
    [finding] = [f for f in fetch_findings(bench.db_path, PR) if f.file == "src/settings.py"]
    [post] = fetch_finding_posts(bench.db_path, PR)
    [root] = [c for c in forge.inline if c["path"] == "src/settings.py"]
    assert post.finding_id == finding.id
    assert (post.comment_id, post.thread_id) == (str(root["id"]), str(root["id"]))
    assert post.review_id == str(root["pull_request_review_id"])
    assert post.provenance == "posted"

    root["line"] = 9
    forge.inline.append(
        {
            "id": 4437509999,
            "in_reply_to_id": root["id"],
            "user": {"login": "jwfaye"},
            "path": "src/settings.py",
            "line": 9,
            "body": f"/resolve {REASON}",
            "created_at": "2026-11-02T11:00:00Z",
            "updated_at": "2026-11-02T11:00:00Z",
        }
    )

    settled = _review(repo, forge, monkeypatch, capsys, dry_run=True)

    assert settled.payload["cached"] is True
    assert settled.payload["owner_resolved"] == [
        {
            "finding_id": finding.id,
            "login": "jwfaye",
            "reason": REASON,
            "comment_id": 4437509999,
            "head_sha": head,
        }
    ]
    assert settled.payload["final_verdict"] == "APPROVE"
    assert settled.exit_code == 0
    assert f"owner-resolved #{finding.id} by jwfaye: {REASON}" in settled.stderr
    row = next(f for f in fetch_findings(bench.db_path, PR) if f.id == finding.id)
    assert row.status is FindingStatus.OWNER_RESOLVED
