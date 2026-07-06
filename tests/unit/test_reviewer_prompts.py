"""Unit tests for SP-REFUTED-FEEDBACK — track record in finder prompts."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)
from ferova.review.reviewer import (
    BotRole,
    Reviewer,
    ReviewVerdict,
    _FailedRunResult,
)


def _rec(
    db: Path,
    *,
    finder: str = "sentinel",
    claim_type: ClaimType = ClaimType.SECURITY,
    status: FindingStatus = FindingStatus.REFUTED,
    file: str = "src/x.py",
    claim: str = "bad claim",
    verification_result: str = "refuter reasoning",
    pr_number: int = 1,
) -> int:
    """Record a single finding into *db* and return its row id."""
    return record_finding(
        db,
        Finding(
            pr_number=pr_number,
            head_sha="head123",
            round=1,
            finder=finder,
            claim_type=claim_type,
            severity=Severity.BLOCKING,
            file=file,
            line_start=1,
            line_end=1,
            claim=claim,
            evidence_pointer=f"{file}:1",
            status=status,
            verification_result=verification_result,
        ),
    )


class _TestReviewer(Reviewer):
    """Tiny Reviewer subclass that returns canned responses and captures the prompt."""

    role = BotRole.SENTINEL

    def _render_prompt(self, diff: str, **kwargs: Any) -> str:
        return "PROMPT"

    def _call_with_retry(
        self,
        prompt: str,
        *,
        pr_number: int | None,
    ) -> tuple[ReviewVerdict, str, list, Any]:
        self._captured_prompt = prompt
        return (ReviewVerdict.APPROVE, "ok", [], _FailedRunResult(error=""))


def test_finder_prompt_carries_its_track_record(tmp_path: Path) -> None:
    """A finder with refutations gets the track record appended; an empty ledger omits it."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _rec(db, finder="sentinel", claim="refuted claim A", verification_result="reason A")
    _rec(db, finder="sentinel", claim="refuted claim B", verification_result="reason B")

    reviewer = _TestReviewer(db_path=db)
    reviewer.review_diff("diff")

    captured = reviewer._captured_prompt
    assert "Your recent refuted claims" in captured
    assert "refuted claim A" in captured
    assert "refuted claim B" in captured
    assert captured.startswith("PROMPT\n\n")

    # Empty ledger → no heading
    db2 = tmp_path / "e.db"
    init_findings_schema(db2)
    reviewer2 = _TestReviewer(db_path=db2)
    reviewer2.review_diff("diff")
    captured2 = reviewer2._captured_prompt
    assert "Your recent refuted claims" not in captured2
    assert captured2 == "PROMPT"
