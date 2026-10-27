"""Integration test: a claim resting on code the refuter was not served is deferred, unjudged.

Drives ``ReviewTeamOrchestrator.review_pr`` end to end (SP-REFUTER-JUDGES-WHAT-IT-CAN
G11) over a real committed git tree under ``tmp_path``, a truthful ``gh`` fake whose
head is that tree's ``HEAD``, REAL ``Architect`` / ``Sentinel`` / ``Tester`` /
``Scribe`` instances whose only replaced surface is ``review_diff``, the real findings
ledger, the real mechanical verifiers and the real refuter with its real evidence
assembly (``git grep`` over the tree, the 40 000-token default context). The Architect
raises a design finding on a cited file that fills the evidence budget, about an
identifier whose only hits live in another file. With the context ceiling set to the
floor (``REPOACH_REFUTER_CONTEXT_CEILING``), the ceiling leaves every hit out, so the
finding must end ``deferred`` with ``evidence exceeds the ceiling (<n> tokens needed,
ceiling 40000)`` while the judge, which would refute it, is never called
(SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G5).
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.forge.github import GitHubForge
from repoach.forge.runner import CommandResult
from repoach.gates.config import REFUTER_CONTEXT_TOKENS_FLOOR
from repoach.review import refuter
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
)
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.refuter_evidence import estimate_tokens
from repoach.review.reviewer import (
    Architect,
    BotRole,
    ReviewComment,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)
from tests.forge_fakes import gh_ok, pr_view_stdout, scripted_github

PR = 41
CITED = "src/table.py"
OTHER = "src/other.py"
IDENTIFIER = "wanted_symbol"
CLAIM = f"`{IDENTIFIER}` duplicates this table and the two drift apart"

_ROLE_TO_CLASS: dict[BotRole, type[Reviewer]] = {
    BotRole.ARCHITECT: Architect,
    BotRole.SENTINEL: Sentinel,
    BotRole.TESTER: Tester,
    BotRole.SCRIBE: Scribe,
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _cited_source(budget_tokens: int) -> str:
    """Return a cited file that fits the evidence budget whole, with too little left for a hit."""
    rows: list[str] = []
    n = 1
    while estimate_tokens("".join(rows)) < budget_tokens - 2_000:
        rows.append(f"row_{n} = '{'x' * 90}'\n")
        n += 1
    return "".join(rows)


def _head_tree(root: Path) -> tuple[str, int]:
    """Commit the cited file and the file holding the identifier; return ``(HEAD, hits)``."""
    probe = Finding(
        pr_number=PR,
        head_sha="probe",
        round=1,
        finder="architect",
        claim_type=ClaimType.DESIGN,
        severity="blocking",
        file=CITED,
        line_start=1,
        line_end=1,
        claim=CLAIM,
        evidence_pointer=f"{CITED}:1",
    )
    budget = refuter._evidence_budget(probe, REFUTER_CONTEXT_TOKENS_FLOOR)
    other = "".join(f"pad_{n} = '{'y' * 300}'\n" for n in range(1, 60))
    other = other.replace("pad_30 = 'y", f"{IDENTIFIER} = '{'z' * 20_000}")
    (root / "src").mkdir(parents=True)
    (root / CITED).write_text(_cited_source(budget), encoding="utf-8")
    (root / OTHER).write_text(other, encoding="utf-8")
    _git(root, "init", "-q", "-b", "feat/table")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "table")
    hits = _git(root, "grep", "-n", "-F", "-w", IDENTIFIER).splitlines()
    return _git(root, "rev-parse", "HEAD"), len(hits)


def _forge(head_sha: str) -> GitHubForge:
    def _run(args: list[str]) -> CommandResult:
        if args[:2] == ["pr", "diff"]:
            return gh_ok(
                f"diff --git a/{CITED} b/{CITED}\n--- a/{CITED}\n+++ b/{CITED}\n"
                "@@ -1 +1 @@\n+row_1 = 1\n",
                argv=args,
            )
        if args[:2] == ["pr", "view"]:
            return gh_ok(
                pr_view_stdout(PR, head_ref="feat/table", head_sha=head_sha, title="table"),
                argv=args,
            )
        if args[:3] == ["api", "--paginate", "--slurp"]:
            return gh_ok("[[]]", argv=args)
        return gh_ok("{}", argv=args)

    return scripted_github(run=_run)


def _outcome(role: BotRole, comments: list[ReviewComment]) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="ok",
        comments=comments,
        model_used="test-model",
        elapsed_s=0.01,
        tokens_used=1,
        raw_response="{}",
    )


def _install_reviewers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Real reviewer instances; only ``review_diff`` is replaced, the Architect raising the claim."""
    comments = {
        BotRole.ARCHITECT: [ReviewComment(file=CITED, line=1, severity="major", body=CLAIM)],
    }
    for role, cls in _ROLE_TO_CLASS.items():
        outcome = _outcome(role, comments.get(role, []))

        def _build(db_path=None, role=role, outcome=outcome) -> Reviewer:
            instance = _ROLE_TO_CLASS[role](loop=MagicMock())
            instance.review_diff = lambda *a, _o=outcome, **kw: _o
            return instance

        monkeypatch.setattr(f"repoach.review.orchestrator.{cls.__name__}", _build)


def _judge_that_must_not_run(calls: list[str]) -> Callable[[], Callable[[str], str]]:
    def _factory() -> Callable[[str], str]:
        def _judge(prompt: str) -> str:
            calls.append(prompt)
            return 'VERDICT: {"verdict": "refuted", "reasoning": "no duplicate in view"}'

        return _judge

    return _factory


def test_unserved_identifier_is_deferred_through_the_review_without_a_judge_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_TOKENS", raising=False)
    monkeypatch.setenv("REPOACH_REFUTER_CONTEXT_CEILING", str(REFUTER_CONTEXT_TOKENS_FLOOR))
    root = tmp_path / "repo"
    root.mkdir()
    head, hits = _head_tree(root)
    assert hits == 1
    _install_reviewers(monkeypatch)
    calls: list[str] = []
    real_judge_for_pr = refuter.judge_findings_for_pr
    monkeypatch.setattr(
        "repoach.review.orchestrator.judge_findings_for_pr",
        lambda *a, **kw: real_judge_for_pr(*a, judge_factory=_judge_that_must_not_run(calls), **kw),
    )
    db = tmp_path / "review.db"
    orch = ReviewTeamOrchestrator(
        forge=_forge(head),
        db_path=db,
        post_to_github=False,
        max_workers=2,
        repo_root=root,
    )

    team = orch.review_pr(pr_number=PR)

    assert calls == []
    judged = [r for r in team.refutations if r.file == CITED]
    assert len(judged) == 1
    outcome = judged[0]
    assert outcome.state == "deferred"
    assert outcome.reason.startswith("evidence exceeds the ceiling (")
    assert outcome.reason.endswith(f" tokens needed, ceiling {REFUTER_CONTEXT_TOKENS_FLOOR})")
    assert outcome.budget_used == REFUTER_CONTEXT_TOKENS_FLOOR
    assert outcome.evidence and outcome.evidence[0].startswith(CITED)
    assert any(
        item.startswith(f"search {IDENTIFIER}: hit {OTHER}:30")
        for item in outcome.evidence_truncated
    )
    assert 0 < outcome.evidence_tokens <= REFUTER_CONTEXT_TOKENS_FLOOR
    assert "refuted 0" in (team.refuter_summary or "")
    assert "deferred 1 (evidence exceeds the ceiling (" in (team.refuter_summary or "")
    assert fetch_findings(db, PR, status=FindingStatus.REFUTED) == []
    assert [f.file for f in fetch_findings(db, PR, status=FindingStatus.PROPOSED)] == [CITED]
