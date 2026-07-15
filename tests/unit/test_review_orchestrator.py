"""Tests for the orchestrator's fail-closed review-ledger recording.

SP-FINDINGS-WRITE-FAIL-CLOSED (audit 2026-07-13 finding H3): the
review-integrity row must be bound to the success of the findings
write. The boundary fake below simulates the exact transient failure
the audit describes — the findings write raising ``OperationalError``
(database locked) — against a real on-disk SQLite ledger, then asserts
through the REAL merge gate that the head cannot read as reviewed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from ferova.review import orchestrator
from ferova.review.merge_gate import (
    compute_merge_decision,
    gather_merge_facts,
    verdict_from_facts,
)
from ferova.review.orchestrator import record_review_ledger
from ferova.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict


def _bench() -> list[ReviewerOutcome]:
    return [
        ReviewerOutcome(role=role, verdict=ReviewVerdict.APPROVE, summary="clean pass")
        for role in (BotRole.ARCHITECT, BotRole.SENTINEL, BotRole.TESTER, BotRole.SCRIBE)
    ]


def test_failed_findings_write_poisons_integrity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = tmp_path / "ledger.db"

    def _locked(*args: object, **kwargs: object) -> int:
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(orchestrator, "record_findings_for_outcomes", _locked)
    recorded = record_review_ledger(
        db, pr_number=7, head_sha="head123", outcomes=_bench(), round_n=1, diff=""
    )
    assert recorded is False

    facts = gather_merge_facts(
        db, pr_number=7, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_complete is False
    assert facts.open_blocking_findings == 0
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES
    assert compute_merge_decision(facts).merge is False


def test_successful_write_keeps_head_approvable(tmp_path: Path) -> None:
    db = tmp_path / "ledger.db"
    recorded = record_review_ledger(
        db, pr_number=7, head_sha="head123", outcomes=_bench(), round_n=1, diff=""
    )
    assert recorded is True

    facts = gather_merge_facts(
        db, pr_number=7, repo_root=tmp_path, head_sha="head123", ci_green=True
    )
    assert facts.review_complete is True
    assert verdict_from_facts(facts) is ReviewVerdict.APPROVE
