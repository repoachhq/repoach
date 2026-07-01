"""Unit tests for SP-REFUTER — adversarial judging of design/security findings.

The judge is injected (no live LLM): every branch of refute_finding and
judge_findings_for_pr is pinned, plus the JSON-verdict parser and the
ledger transitions. The judge_factory is called lazily — a PR with no
judged findings never builds a judge.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from ferova.review.refuter import (
    _parse_verdict,
    judge_findings_for_pr,
    refute_finding,
)


def _finding(claim_type: ClaimType, *, file: str = "src/m.py", claim: str = "smell") -> Finding:
    return Finding(
        pr_number=1,
        head_sha="abc1234",
        round=1,
        finder="architect",
        claim_type=claim_type,
        severity="blocking",
        file=file,
        line_start=2,
        line_end=2,
        claim=claim,
        evidence_pointer=f"{file}:2",
    )


def _seed_file(repo: Path, rel: str = "src/m.py") -> None:
    (repo / "src").mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text("a = 1\nb = 2\nc = 3\n", encoding="utf-8")


def test_parse_verdict_extracts_refuted_bool() -> None:
    assert _parse_verdict('{"refuted": true, "reasoning": "vague"}') == (True, "vague")
    assert _parse_verdict('prefix {"refuted": false, "reasoning": "real"} suffix') == (
        False,
        "real",
    )


def test_parse_verdict_rejects_bad_shapes() -> None:
    assert _parse_verdict("no json here") is None
    assert _parse_verdict('{"refuted": "yes"}') is None
    assert _parse_verdict("{not json}") is None


def _fixed_judge(reply: str):
    def _judge(_prompt: str) -> str:
        return reply

    return _judge


def test_refute_finding_refuted(tmp_path: Path) -> None:
    _seed_file(tmp_path)
    judge = _fixed_judge('{"refuted": true, "reasoning": "evidence shows no defect"}')
    status, reasoning = refute_finding(_finding(ClaimType.DESIGN), repo_root=tmp_path, judge=judge)
    assert status is FindingStatus.REFUTED
    assert "no defect" in reasoning


def test_refute_finding_verified(tmp_path: Path) -> None:
    _seed_file(tmp_path)
    judge = _fixed_judge('{"refuted": false, "reasoning": "real exposure"}')
    status, _ = refute_finding(_finding(ClaimType.SECURITY), repo_root=tmp_path, judge=judge)
    assert status is FindingStatus.VERIFIED


def test_refute_finding_proposed_on_missing_evidence(tmp_path: Path) -> None:
    status, reason = refute_finding(
        _finding(ClaimType.DESIGN, file="src/ghost.py"),
        repo_root=tmp_path,
        judge=_fixed_judge('{"refuted": true}'),
    )
    assert status is FindingStatus.PROPOSED
    assert "evidence" in reason


def test_refute_finding_proposed_on_unparseable_verdict(tmp_path: Path) -> None:
    _seed_file(tmp_path)
    status, _ = refute_finding(
        _finding(ClaimType.DESIGN), repo_root=tmp_path, judge=_fixed_judge("I think it's fine")
    )
    assert status is FindingStatus.PROPOSED


def test_refute_finding_proposed_when_judge_raises(tmp_path: Path) -> None:
    _seed_file(tmp_path)

    def _boom(_p: str) -> str:
        raise RuntimeError("chain exhausted")

    status, reason = refute_finding(_finding(ClaimType.DESIGN), repo_root=tmp_path, judge=_boom)
    assert status is FindingStatus.PROPOSED
    assert "judge" in reason


def test_judge_findings_for_pr_transitions_and_counts(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    _seed_file(tmp_path)
    record_finding(db, _finding(ClaimType.DESIGN, claim="layering"))
    record_finding(db, _finding(ClaimType.SECURITY, claim="injection"))
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="no test"))

    calls = {"n": 0}

    def _factory():
        calls["n"] += 1
        return lambda _p: '{"refuted": true, "reasoning": "no concrete defect"}'

    counts = judge_findings_for_pr(
        db, pr_number=1, repo_root=tmp_path, head_sha="dead123", judge_factory=_factory
    )
    assert counts == {"verified": 0, "refuted": 2, "deferred": 0}
    assert calls["n"] == 1
    refuted = fetch_findings(db, 1, status=FindingStatus.REFUTED)
    assert {f.claim_type for f in refuted} == {ClaimType.DESIGN, ClaimType.SECURITY}
    assert refuted[0].verification_method == "refuter"
    assert len(fetch_findings(db, 1, status=FindingStatus.PROPOSED)) == 1


def test_judge_findings_no_targets_never_builds_judge(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(db, _finding(ClaimType.MISSING_TEST, claim="no test"))

    built = {"n": 0}

    def _factory():
        built["n"] += 1
        return lambda _p: "{}"

    counts = judge_findings_for_pr(
        db, pr_number=1, repo_root=tmp_path, head_sha=None, judge_factory=_factory
    )
    assert counts == {"verified": 0, "refuted": 0, "deferred": 0}
    assert built["n"] == 0
