"""Unit tests for SP-STUCK-ESCALATION — slice 9 stuck detection + escalation.

Pins the pure :func:`assess_stuck` cap / stall / progress branches, the
``pr_coder_rounds`` round ledger round-trip, the ``open -> stuck``
transition (open blocking findings only), and the escalation dossier
shape. Thresholds are imported, never hardcoded.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
    update_finding_status,
)
from repoach.review.stuck import (
    MAX_CODER_ROUNDS,
    PROPOSED_ESCALATION_ATTEMPTS,
    STALL_WINDOW,
    CoderRound,
    assess_proposed_escalation,
    assess_stuck,
    build_proposed_escalation_dossier,
    build_stuck_dossier,
    fetch_coder_rounds,
    init_stuck_schema,
    mark_findings_stuck,
    record_coder_round,
    select_newly_escalated,
)


def _round(after: int, *, index: int = 1, before: int = 5) -> CoderRound:
    return CoderRound(round_index=index, open_blocking_before=before, open_blocking_after=after)


def _open_blocking(db: Path, *, claim: str = "smell", severity: str = "blocking") -> int:
    finding = Finding(
        pr_number=1,
        head_sha="head123",
        round=1,
        finder="architect",
        claim_type=ClaimType.DESIGN,
        severity=severity,
        file="src/m.py",
        line_start=3,
        line_end=3,
        claim=claim,
        evidence_pointer="src/m.py:3",
    )
    fid = record_finding(db, finding)
    update_finding_status(
        db, fid, FindingStatus.VERIFIED, verification_method="refuter", checked_at_sha="head123"
    )
    update_finding_status(db, fid, FindingStatus.OPEN, verification_method="refuter")
    return fid


def test_assess_stuck_empty() -> None:
    assert assess_stuck([]).stuck is False


def test_assess_stuck_cap() -> None:
    rounds = [_round(2, index=i + 1) for i in range(MAX_CODER_ROUNDS)]
    assessment = assess_stuck(rounds)
    assert assessment.stuck is True
    assert "cap" in assessment.reason
    assert assessment.n_rounds == MAX_CODER_ROUNDS


def test_assess_stuck_stall() -> None:
    rounds = [_round(2, index=i + 1) for i in range(STALL_WINDOW)]
    assessment = assess_stuck(rounds)
    assert assessment.stuck is True
    assert "progress" in assessment.reason


def test_assess_stuck_progress_not_stuck() -> None:
    rounds = [_round(4, index=1), _round(2, index=2)]
    assert assess_stuck(rounds).stuck is False


def test_assess_stuck_reaching_zero_not_stuck() -> None:
    rounds = [_round(3, index=1), _round(0, index=2)]
    assert assess_stuck(rounds).stuck is False


def test_record_and_fetch_coder_rounds(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_stuck_schema(db)
    record_coder_round(db, pr_number=1, open_blocking_before=3, open_blocking_after=2)
    record_coder_round(db, pr_number=2, open_blocking_before=9, open_blocking_after=9)
    rounds = fetch_coder_rounds(db, 1)
    assert len(rounds) == 1
    assert rounds[0].open_blocking_before == 3
    assert rounds[0].open_blocking_after == 2


def test_round_index_increments(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_stuck_schema(db)
    first = record_coder_round(db, pr_number=1, open_blocking_before=3, open_blocking_after=2)
    second = record_coder_round(db, pr_number=1, open_blocking_before=2, open_blocking_after=1)
    assert (first, second) == (1, 2)
    assert [r.round_index for r in fetch_coder_rounds(db, 1)] == [1, 2]


def test_mark_findings_stuck_targets_open_blocking(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _open_blocking(db, claim="real blocker")
    advisory = record_finding(
        db,
        Finding(
            pr_number=1,
            head_sha="head123",
            round=1,
            finder="scribe",
            claim_type=ClaimType.MISSING_DOCSTRING,
            severity="advisory",
            file="src/m.py",
            line_start=1,
            line_end=1,
            claim="nit",
            evidence_pointer="src/m.py:1",
        ),
    )
    update_finding_status(db, advisory, FindingStatus.VERIFIED, verification_method="x")

    marked = mark_findings_stuck(db, 1)

    assert len(marked) == 1
    stuck = fetch_findings(db, 1, status=FindingStatus.STUCK)
    assert len(stuck) == 1
    assert stuck[0].claim == "real blocker"


def _proposed_finding(
    *,
    fid: int | None = 1,
    attempts: int = 0,
    severity: str = "blocking",
    status: FindingStatus = FindingStatus.PROPOSED,
    method: str = "",
    result: str = "",
) -> Finding:
    return Finding(
        id=fid,
        pr_number=1,
        head_sha="head123",
        round=1,
        finder="architect",
        claim_type=ClaimType.MISSING_TEST,
        severity=severity,
        file="src/m.py",
        line_start=3,
        line_end=3,
        claim="missing test for foo",
        evidence_pointer="src/m.py:3",
        status=status,
        verification_method=method,
        verification_result=result,
        verify_attempts=attempts,
    )


def test_assess_proposed_escalation_thresholds() -> None:
    below = _proposed_finding(attempts=PROPOSED_ESCALATION_ATTEMPTS - 1)
    at_threshold = _proposed_finding(fid=2, attempts=PROPOSED_ESCALATION_ATTEMPTS)
    advisory = _proposed_finding(
        fid=3,
        attempts=PROPOSED_ESCALATION_ATTEMPTS,
        severity="advisory",
    )
    not_proposed = _proposed_finding(
        fid=4,
        attempts=PROPOSED_ESCALATION_ATTEMPTS,
        status=FindingStatus.VERIFIED,
    )

    assert assess_proposed_escalation([below]) == []
    assert assess_proposed_escalation([at_threshold]) == [at_threshold]
    assert assess_proposed_escalation([advisory]) == []
    assert assess_proposed_escalation([not_proposed]) == []


def test_proposed_escalation_dossier_shape() -> None:
    finding = _proposed_finding(
        fid=7,
        attempts=PROPOSED_ESCALATION_ATTEMPTS,
        method="symbol_search",
        result="no checkable symbol",
    )

    dossier = build_proposed_escalation_dossier([finding])

    assert dossier["kind"] == "proposed_escalation"
    assert len(dossier["findings"]) == 1
    entry = dossier["findings"][0]
    assert entry["id"] == 7
    assert entry["claim"] == "missing test for foo"
    assert entry["claim_type"] == "missing_test"
    assert entry["verify_attempts"] == PROPOSED_ESCALATION_ATTEMPTS
    assert entry["verification_method"] == "symbol_search"
    assert entry["verification_result"] == "no checkable symbol"


def test_proposed_escalation_fires_once() -> None:
    at_threshold = _proposed_finding(
        fid=11,
        attempts=PROPOSED_ESCALATION_ATTEMPTS,
    )
    past_threshold = _proposed_finding(
        fid=12,
        attempts=PROPOSED_ESCALATION_ATTEMPTS + 1,
    )

    assert select_newly_escalated([at_threshold]) == [at_threshold]
    assert select_newly_escalated([past_threshold]) == []


def test_build_stuck_dossier_shape(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _open_blocking(db, claim="cannot fix this")
    stuck_findings = mark_findings_stuck(db, 1)
    rounds = [_round(2, index=1), _round(2, index=2)]

    dossier = build_stuck_dossier(
        pr_number=1,
        reason="no progress over last 2 rounds",
        rounds=rounds,
        stuck_findings=stuck_findings,
    )

    assert dossier["kind"] == "stuck_escalation"
    assert dossier["pr_number"] == 1
    assert dossier["n_rounds"] == len(rounds)
    assert len(dossier["trajectory"]) == len(rounds)
    assert dossier["stuck_findings"][0]["claim"] == "cannot fix this"


def test_orchestrator_escalation_seam_is_guarded(monkeypatch) -> None:
    """The proposed-escalation dossier seam is settings-guarded.

    With routine settings absent, firing the dossier is a silent no-op
    — the monkeypatched transport is never called.
    """
    from types import SimpleNamespace

    from repoach.review import orchestrator
    from repoach.review.findings import (
        ClaimType,
        Finding,
        FindingStatus,
        init_findings_schema,
        record_finding,
    )

    SimpleNamespace()
    init_findings_schema(Path("/tmp/_proposed_escalation_seam.db"))
    record_finding(
        Path("/tmp/_proposed_escalation_seam.db"),
        Finding(
            pr_number=1,
            head_sha="head123",
            round=1,
            finder="architect",
            claim_type=ClaimType.MISSING_TEST,
            severity="blocking",
            file="src/m.py",
            line_start=3,
            line_end=3,
            claim="missing test for foo",
            evidence_pointer="src/m.py:3",
            status=FindingStatus.PROPOSED,
            verify_attempts=PROPOSED_ESCALATION_ATTEMPTS,
        ),
    )

    monkeypatch.setattr(
        orchestrator,
        "get_settings",
        lambda: SimpleNamespace(
            claude_code_routine_id=None,
            claude_code_routine_token=None,
        ),
    )

    called = {"count": 0}

    def _fake_fire(*, routine_id: str, token: str, payload: dict) -> SimpleNamespace:
        called["count"] += 1
        return SimpleNamespace(
            ok=True, status_code=200, session_id="s", session_url="u", error=None
        )

    monkeypatch.setattr(orchestrator, "fire_review_routine", _fake_fire)

    orchestrator.ReviewTeamOrchestrator._fire_proposed_escalation_dossier(
        SimpleNamespace(_db_path=Path("/tmp/_proposed_escalation_seam.db")),
        pr_number=1,
    )

    assert called["count"] == 0
