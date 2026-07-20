"""Integration test for the CI-findings wire (SP-CI-FINDINGS-WIRE).

Pins the end-to-end red-to-blocked-to-green-to-merge-ready path through
the findings ledger and merge gate: a red CI materialises as a verified
blocking broken_behavior finding, the ledger reports ci_green=False and
BLOCKED, the Coder opens the finding, then resolve_broken_behavior_findings
closes it at a new green head, flipping the ledger back to ci_green=True
with zero open blocking findings.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.coder_findings import (
    open_verified_blocking,
    record_ci_failures_as_findings,
    resolve_broken_behavior_findings,
)
from repoach.review.merge_gate import compute_merge_decision, summarise_ledger_facts
from repoach.review.report import render_ledger_report


def test_red_ci_finding_flips_ledger_to_blocked_then_resolves_to_merge_ready(
    tmp_path: Path,
) -> None:
    """End-to-end: red CI -> BLOCKED -> resolve -> MERGE-READY.

    Steps through the full lifecycle a CI-failure finding travels in the
    real coder-loop: materialise, open, resolve at a new green head, and
    verify the ledger and report reflect each state correctly.
    """
    db = tmp_path / "ci_wire.db"

    failed_rows = [{"name": "Test suite", "link": "https://x/runs/1/job/2"}]

    created = record_ci_failures_as_findings(
        db, pr_number=53, head_sha="shaA", failed_rows=failed_rows
    )
    assert created == 1

    facts = summarise_ledger_facts(db, pr_number=53, head_sha="shaA")
    assert facts.ci_green is False

    decision = compute_merge_decision(facts)
    assert decision.merge is False

    body = render_ledger_report(db, pr_number=53, decision=decision, facts=facts)
    assert "Decision: BLOCKED" in body
    assert "| CI green | False |" in body

    opened = open_verified_blocking(db, 53, head_sha="shaA")
    assert opened == 1

    resolved = resolve_broken_behavior_findings(db, pr_number=53, head_sha="shaB")
    assert resolved == 1

    facts2 = summarise_ledger_facts(db, pr_number=53, head_sha="shaB")
    assert facts2.ci_green is True
    assert facts2.open_blocking_findings == 0
