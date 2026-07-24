"""Unit tests for SP-VERIFIER-LIB — mechanical finding verification.

Each verifier's verified/refuted/proposed branch is pinned against a
real tmp repo + tmp ledger; the orchestrator wiring is asserted to run
in dual-run (records the verified/refuted split, never breaks a review
when the verifier raises).
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.finding_verifiers import verify_finding, verify_findings_for_pr
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
)


def _finding(claim_type: ClaimType, *, claim: str = "", file: str = "") -> Finding:
    return Finding(
        pr_number=1,
        head_sha="abc1234",
        round=1,
        finder="tester",
        claim_type=claim_type,
        severity="blocking",
        file=file,
        line_start=1,
        line_end=1,
        claim=claim,
        evidence_pointer=f"{file}:1",
    )


def test_missing_test_refuted_when_symbol_exists(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_widget_renders():\n    assert True\n", encoding="utf-8"
    )
    status, method, _ = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="missing test_widget_renders"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.REFUTED
    assert method == "symbol_search"


def test_missing_test_verified_when_absent(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    status, _, _ = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="missing test_never_written"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.VERIFIED


def test_missing_test_proposed_without_symbol(tmp_path: Path) -> None:
    status, _, result = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="tests are weak here"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.PROPOSED
    assert "no checkable symbol" in result


def test_missing_test_not_refuted_by_src_symbol(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "verdicts.py").write_text(
        "def _parse_verdict(text):\n    return text\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    status, method, _ = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="no test for `_parse_verdict`"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.VERIFIED
    assert method == "symbol_search"


def test_missing_test_refuted_by_real_tests_dir_test(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "verdicts.py").write_text(
        "def _parse_verdict(text):\n    return text\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_verdicts.py").write_text(
        "def test_parse_verdict():\n    assert True\n", encoding="utf-8"
    )
    status, _, _ = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="no test for `_parse_verdict`"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.REFUTED


def test_src_planted_test_does_not_refute(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "planted.py").write_text(
        "def test_foo():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    status, _, _ = verify_finding(
        _finding(ClaimType.MISSING_TEST, claim="no test for `test_foo`"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.VERIFIED


def test_missing_docstring_refuted_when_documented(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        '"""Module."""\n\n\ndef public():\n    """Doc."""\n    return 1\n', encoding="utf-8"
    )
    status, _, _ = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, file="src/m.py"), repo_root=tmp_path
    )
    assert status is FindingStatus.REFUTED


def test_missing_docstring_verified_when_undocumented(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text(
        '"""Module."""\n\n\ndef public():\n    return 1\n', encoding="utf-8"
    )
    status, _, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, file="src/m.py"), repo_root=tmp_path
    )
    assert status is FindingStatus.VERIFIED
    assert "1 public symbol" in result


def test_missing_docstring_proposed_when_file_absent(tmp_path: Path) -> None:
    status, _, _ = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, file="src/ghost.py"), repo_root=tmp_path
    )
    assert status is FindingStatus.PROPOSED


def test_missing_docstring_refuted_on_exempt_path(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8"
    )
    status, _, result = verify_finding(
        _finding(ClaimType.MISSING_DOCSTRING, file="tests/unit/test_x.py"),
        repo_root=tmp_path,
    )
    assert status is FindingStatus.REFUTED
    assert "exempt" in result


def test_lint_verified_on_violation(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "dirty.py").write_text("x = 1  # inline comment\n", encoding="utf-8")
    status, _, _ = verify_finding(
        _finding(ClaimType.LINT_CONVENTION, file="src/dirty.py"), repo_root=tmp_path
    )
    assert status is FindingStatus.VERIFIED


def test_lint_refuted_when_clean(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "clean.py").write_text('"""Clean."""\n\nx = 1\n', encoding="utf-8")
    status, _, _ = verify_finding(
        _finding(ClaimType.LINT_CONVENTION, file="src/clean.py"), repo_root=tmp_path
    )
    assert status is FindingStatus.REFUTED


def test_design_finding_deferred(tmp_path: Path) -> None:
    status, method, _ = verify_finding(_finding(ClaimType.DESIGN), repo_root=tmp_path)
    assert status is FindingStatus.PROPOSED
    assert method == "deferred"


def test_verify_findings_for_pr_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_present():\n    assert True\n", encoding="utf-8"
    )
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="missing test_present"))
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="missing test_absent_one"))
    record_finding(db, _finding(ClaimType.DESIGN, claim="layering smell"))

    counts = verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="dead123")
    assert counts == {"verified": 1, "refuted": 1, "deferred": 1}

    refuted = fetch_findings(db, 1, status=FindingStatus.REFUTED)
    assert len(refuted) == 1
    assert refuted[0].checked_at_sha == "dead123"
    assert len(fetch_findings(db, 1, status=FindingStatus.PROPOSED)) == 1


def test_deferred_verification_persists_diagnostic(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            claim="tests are weak here",
            file="src/widget.py",
        ),
    )

    counts = verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="head123")
    assert counts == {"verified": 0, "refuted": 0, "deferred": 1}

    proposed = fetch_findings(db, 1, status=FindingStatus.PROPOSED)
    assert len(proposed) == 1
    assert proposed[0].verification_method == "symbol_search"
    assert proposed[0].verification_result == "no checkable symbol"
    assert proposed[0].verify_attempts == 1

    counts = verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="head456")
    assert counts == {"verified": 0, "refuted": 0, "deferred": 1}

    proposed = fetch_findings(db, 1, status=FindingStatus.PROPOSED)
    assert len(proposed) == 1
    assert proposed[0].verify_attempts == 2
