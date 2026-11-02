"""SP-OWNER-SETTLES-A-FINDING AC: an owner's ``/resolve`` flips ``review pr`` to APPROVE at the same head.

A real bare origin carries ``refs/pull/7/head``; the ledger holds one
blocking ``spec_gap`` finding raised at an older head, citing line 0 of a
file the head still has, the shape of Yoob !339's blockers: the refuter
cannot be served that evidence and the re-check at head defers it
(``needs judgement``). ``review pr --dry-run`` at the head, through the
real :class:`~repoach.forge.github.GitHubForge` with a stubbed ``gh``,
ends REQUEST_CHANGES. A maintainer then comments ``/resolve #<id>
<reason>`` on the pull request; the next ``review pr`` at the same head
(a replay of the stored run) applies it, prints the owner line, carries
it in the JSON, leaves the answer unposted because the run is a dry
run, and ends APPROVE.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult
from repoach.review import orchestrator
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
from repoach.review.orchestrator import run_review
from tests.fixtures.generated_artefacts.harness import Forge, Repo, Review, install

PR = 7
OLDER = "0cc9008" + "0" * 33
REASON = "environment review-gate is protected: deploy access Maintainers, read back from the API"


class DeferringJudge:
    """A refuter that cannot see the fact the finding states, so it defers; counts its calls."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        reasoning = "the environment protection is not visible in the evidence window"
        return "VERDICT: " + json.dumps({"refuted": True, "reasoning": reasoning})


class CommentingForge(Forge):
    """The harness forge, serving the pull request's comments and a maintainer's permission."""

    def __init__(self, repo: Repo, head: str, comments: list[dict[str, object]]) -> None:
        super().__init__(repo, PR, head)
        self._comments = comments
        self.permission_reads: list[str] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        if args[:3] == ["api", "--paginate", "--slurp"] and args[3].endswith(
            f"/issues/{PR}/comments"
        ):
            return GhResult(0, json.dumps([self._comments]), "", argv=args)
        if len(args) == 2 and args[0] == "api" and args[1].endswith("/permission"):
            login = args[1].split("/")[-2]
            self.permission_reads.append(login)
            role = "maintain" if login == "jwfaye" else "read"
            return GhResult(
                0, json.dumps({"permission": "write", "role_name": role}), "", argv=args
            )
        return super()._run(args, input_data=input_data)


def _review(
    repo: Repo,
    forge: CommentingForge,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Review:
    """Run ``review pr 7 --dry-run`` from the checkout through ``forge``."""
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(review_cmds, "run_review", functools.partial(run_review, forge=forge))
    exit_code = 0
    capsys.readouterr()
    try:
        review_cmds.review_pr(
            PR,
            dry_run=True,
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
    return Review(json.loads(out), err.splitlines(), exit_code)


def test_owner_resolve_flips_the_verdict_at_the_same_head(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A deferred blocking finding holds the head; the owner's ``/resolve`` releases it."""
    bench = install(monkeypatch, tmp_path)
    judge = DeferringJudge()
    monkeypatch.setattr(
        orchestrator,
        "reverify_older_findings",
        functools.partial(reverify_older_findings, judge_factory=lambda: judge),
    )
    repo = Repo(tmp_path / "yoob", {"ci/review-gate.sh": "#!/bin/sh\necho gate\n"})
    head = repo.push_head({"ci/review-gate.sh": "#!/bin/sh\necho gate\necho evidence\n"})
    init_findings_schema(bench.db_path)
    blocker = record_finding(
        bench.db_path,
        Finding(
            pr_number=PR,
            head_sha=OLDER,
            round=1,
            finder="sentinel",
            claim_type=ClaimType.SPEC_GAP,
            severity=Severity.BLOCKING,
            file="ci/review-gate.sh",
            line_start=0,
            line_end=0,
            claim="the review-gate environment is not protected on GitLab",
            evidence_pointer="ci/review-gate.sh:0",
            status=FindingStatus.VERIFIED,
            verification_method="refuter",
            verification_result="confirmed at 0cc9008",
            checked_at_sha=OLDER,
        ),
    )

    first = _review(repo, CommentingForge(repo, head, []), monkeypatch, capsys)

    assert first.exit_code == 2
    assert first.payload["final_verdict"] == "REQUEST_CHANGES"
    assert first.payload["verdict_basis"] == [blocker]
    assert first.payload["owner_resolved"] == []
    assert first.payload["reverified"]["needs_judgement"] == 1
    assert judge.calls == 1

    comment = {
        "id": 501,
        "user": {"login": "jwfaye"},
        "body": f"/resolve #{blocker} {REASON}",
        "created_at": "2026-11-01T12:00:00Z",
        "updated_at": "2026-11-01T12:00:00Z",
    }
    owner = CommentingForge(repo, head, [comment])
    second = _review(repo, owner, monkeypatch, capsys)

    assert second.payload["head_sha"] == head
    assert second.payload["cached"] is True
    assert second.payload["final_verdict"] == "APPROVE"
    assert second.exit_code == 0
    assert second.payload["verdict_basis"] == []
    assert second.payload["owner_resolved"] == [
        {
            "finding_id": blocker,
            "login": "jwfaye",
            "reason": REASON,
            "comment_id": 501,
            "head_sha": head,
        }
    ]
    assert second.payload["owner_commands"][0]["outcome"] == "applied"
    assert f"owner-resolved #{blocker} by jwfaye: {REASON}" in second.stderr
    assert any(line.startswith("owner reply not posted: ") for line in second.stderr)
    assert second.payload["refused_writes"] == 0
    assert owner.permission_reads == ["jwfaye"]
    assert judge.calls == 1
    row = next(f for f in fetch_findings(bench.db_path, PR) if f.id == blocker)
    assert row.status is FindingStatus.OWNER_RESOLVED
