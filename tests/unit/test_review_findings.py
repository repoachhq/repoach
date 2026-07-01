"""Unit tests for the Finding model, claim taxonomy, lifecycle law, and pr_findings ledger."""

from __future__ import annotations

from pathlib import Path

from structlog.testing import capture_logs

from ferova.review.findings import (
    ALLOWED_TRANSITIONS,
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    is_valid_transition,
    record_finding,
    update_finding_status,
)


def _make_finding(*, pr_number: int = 1) -> Finding:
    return Finding(
        pr_number=pr_number,
        head_sha="abc123",
        round=1,
        finder="test-agent",
        claim_type=ClaimType.MISSING_TEST,
        severity=Severity.BLOCKING,
        file="src/ferova/review/findings.py",
        line_start=1,
        line_end=10,
        claim="Missing unit test coverage.",
        evidence_pointer="",
    )


def test_allowed_transitions_law() -> None:
    """Verify each ALLOWED_TRANSITIONS entry matches the spec diagram."""
    assert ALLOWED_TRANSITIONS[FindingStatus.PROPOSED] == frozenset(
        {FindingStatus.VERIFIED, FindingStatus.REFUTED}
    )
    assert ALLOWED_TRANSITIONS[FindingStatus.VERIFIED] == frozenset({FindingStatus.OPEN})
    assert ALLOWED_TRANSITIONS[FindingStatus.OPEN] == frozenset(
        {FindingStatus.RESOLVED, FindingStatus.STUCK}
    )
    assert ALLOWED_TRANSITIONS[FindingStatus.REFUTED] == frozenset()
    assert ALLOWED_TRANSITIONS[FindingStatus.RESOLVED] == frozenset()
    assert ALLOWED_TRANSITIONS[FindingStatus.STUCK] == frozenset()


def test_terminal_states_have_no_exits() -> None:
    """Refuted, resolved, and stuck must map to empty frozensets."""
    terminal = {FindingStatus.REFUTED, FindingStatus.RESOLVED, FindingStatus.STUCK}
    for state in terminal:
        assert ALLOWED_TRANSITIONS[state] == frozenset(), (
            f"{state} should be terminal but has exits"
        )


def test_is_valid_transition_accepts_legal_move() -> None:
    """proposed -> verified is a legal transition."""
    assert is_valid_transition(FindingStatus.PROPOSED, FindingStatus.VERIFIED) is True


def test_is_valid_transition_rejects_illegal_move() -> None:
    """proposed -> open skips verified and must be rejected."""
    assert is_valid_transition(FindingStatus.PROPOSED, FindingStatus.OPEN) is False


def test_finding_default_status_is_proposed() -> None:
    """A Finding constructed without an explicit status defaults to PROPOSED."""
    finding = Finding(
        pr_number=1,
        head_sha="abc123",
        round=1,
        finder="test-agent",
        claim_type=ClaimType.MISSING_TEST,
        severity=Severity.BLOCKING,
        file="src/ferova/review/findings.py",
        line_start=1,
        line_end=10,
        claim="Missing unit test coverage.",
        evidence_pointer="",
    )
    assert finding.status == FindingStatus.PROPOSED


def test_finding_requires_claim_fields() -> None:
    """Construction with all required fields succeeds and values are accessible."""
    finding = Finding(
        pr_number=42,
        head_sha="deadbeef",
        round=2,
        finder="nim-reviewer",
        claim_type=ClaimType.MISSING_TEST,
        severity=Severity.BLOCKING,
        file="src/ferova/review/findings.py",
        line_start=1,
        line_end=10,
        claim="No test for the greet() helper.",
        evidence_pointer="ci/logs/run-42.txt",
    )
    assert finding.pr_number == 42
    assert finding.head_sha == "deadbeef"
    assert finding.round == 2
    assert finding.finder == "nim-reviewer"
    assert finding.claim_type == ClaimType.MISSING_TEST
    assert finding.severity == Severity.BLOCKING
    assert finding.file == "src/ferova/review/findings.py"
    assert finding.line_start == 1
    assert finding.line_end == 10
    assert finding.claim == "No test for the greet() helper."
    assert finding.evidence_pointer == "ci/logs/run-42.txt"


def test_init_findings_schema_idempotent(tmp_path: Path) -> None:
    """Calling init_findings_schema twice does not raise."""
    db_path = tmp_path / "test.db"
    init_findings_schema(db_path)
    init_findings_schema(db_path)


def test_record_and_fetch_finding_round_trip(tmp_path: Path) -> None:
    """Insert a Finding then fetch it back; all scalar fields must match."""
    db_path = tmp_path / "test.db"
    init_findings_schema(db_path)
    finding = _make_finding()
    finding_id = record_finding(db_path, finding)
    results = fetch_findings(db_path, finding.pr_number)
    assert len(results) == 1
    f = results[0]
    assert f.id == finding_id
    assert f.pr_number == finding.pr_number
    assert f.head_sha == finding.head_sha
    assert f.round == finding.round
    assert f.finder == finding.finder
    assert f.claim_type == finding.claim_type
    assert f.severity == finding.severity
    assert f.file == finding.file
    assert f.line_start == finding.line_start
    assert f.line_end == finding.line_end
    assert f.claim == finding.claim
    assert f.evidence_pointer == finding.evidence_pointer
    assert f.status == FindingStatus.PROPOSED


def test_legal_transition_updates(tmp_path: Path) -> None:
    """proposed -> verified returns True and the persisted row reads verified."""
    db_path = tmp_path / "test.db"
    init_findings_schema(db_path)
    finding_id = record_finding(db_path, _make_finding())
    result = update_finding_status(db_path, finding_id, FindingStatus.VERIFIED)
    assert result is True
    findings = fetch_findings(db_path, 1)
    assert findings[0].status == FindingStatus.VERIFIED


def test_illegal_transition_refused(tmp_path: Path) -> None:
    """verified -> resolved (skips open) returns False, row stays verified, warning logged."""
    db_path = tmp_path / "test.db"
    init_findings_schema(db_path)
    finding_id = record_finding(db_path, _make_finding())
    update_finding_status(db_path, finding_id, FindingStatus.VERIFIED)
    with capture_logs() as captured:
        result = update_finding_status(db_path, finding_id, FindingStatus.RESOLVED)
    assert result is False
    findings = fetch_findings(db_path, 1)
    assert findings[0].status == FindingStatus.VERIFIED
    assert any(log.get("log_level") == "warning" for log in captured)


def test_fetch_findings_filters_by_status(tmp_path: Path) -> None:
    """Filtering by status returns only findings in that state."""
    db_path = tmp_path / "test.db"
    init_findings_schema(db_path)
    id_proposed = record_finding(db_path, _make_finding(pr_number=10))
    id_to_verify = record_finding(db_path, _make_finding(pr_number=10))
    update_finding_status(db_path, id_to_verify, FindingStatus.VERIFIED)
    proposed_only = fetch_findings(db_path, 10, status=FindingStatus.PROPOSED)
    assert len(proposed_only) == 1
    assert proposed_only[0].id == id_proposed
    verified_only = fetch_findings(db_path, 10, status=FindingStatus.VERIFIED)
    assert len(verified_only) == 1
    assert verified_only[0].id == id_to_verify
