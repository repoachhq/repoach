"""The review report lists every finding that can move ``final_verdict``.

Measured on Yoob MRs !324 and !327 (2026-10-25): the verdict was
``REQUEST_CHANGES`` on one finding a mechanical verifier had verified,
while ``findings[]`` held only the refuter's outcomes ("8 findings, 8
refuted"). These tests pin the invariant on a fixture with one
mechanically verified finding, two refuter-refuted ones and one deferred
by the judging cap: every id the ledger facts count as open blocking is
one entry of the report, named by its judge.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.finding_verifiers import recorded_verifications, verify_findings_for_pr
from repoach.review.findings import (
    ClaimType,
    Finding,
    init_findings_schema,
    record_finding,
    record_review_integrity,
)
from repoach.review.merge_gate import (
    MergeFacts,
    compute_merge_decision,
    gather_merge_facts,
    summarise_ledger_facts,
    verdict_from_facts,
)
from repoach.review.orchestrator import TeamOutcome, team_outcome_to_dict
from repoach.review.refuter import Refutation, judge_findings_for_pr
from repoach.review.report import (
    findings_table_lines,
    render_ledger_report,
    report_findings,
)
from repoach.review.reviewer import ReviewVerdict

HEAD = "abc1234def5678"


def _finding(claim_type: ClaimType, *, claim: str, line: int, file: str = "src/m.py") -> Finding:
    return Finding(
        pr_number=1,
        head_sha=HEAD,
        round=1,
        finder="architect",
        claim_type=claim_type,
        severity="blocking",
        file=file,
        line_start=line,
        line_end=line,
        claim=claim,
        evidence_pointer=f"{file}:{line}",
    )


def _refuting_judge():
    return lambda _prompt: 'VERDICT: {"refuted": true, "reasoning": "not supported by the code"}'


def _fixture(tmp_path: Path) -> tuple[Path, int]:
    """Seed the ledger and the tree; return the db and the migration finding's id."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    mechanical_id = record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            claim="missing test_migration_upgrade",
            line=1,
            file="src/migration.py",
        ),
    )
    for line in (2, 3, 4):
        record_finding(db, _finding(ClaimType.DESIGN, claim=f"layering smell {line}", line=line))
    record_review_integrity(db, pr_number=1, head_sha=HEAD, n_reviewers=4, n_unparsed=0)
    return db, mechanical_id


def _run(tmp_path: Path):
    db, mechanical_id = _fixture(tmp_path)
    verification = verify_findings_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha=HEAD)
    judging = judge_findings_for_pr(
        db,
        pr_number=1,
        repo_root=tmp_path,
        head_sha=HEAD,
        judge_factory=_refuting_judge,
        max_judged=2,
    )
    facts = summarise_ledger_facts(db, pr_number=1, head_sha=HEAD)
    entries = report_findings(
        db,
        pr_number=1,
        facts=facts,
        verifications=verification.settled,
        refutations=judging.outcomes,
    )
    return db, mechanical_id, verification, judging, facts, entries


def test_every_blocking_finding_counted_is_one_report_entry(tmp_path: Path) -> None:
    _, mechanical_id, verification, judging, facts, entries = _run(tmp_path)

    assert verification.counts == {"verified": 1, "refuted": 0, "deferred": 3}
    assert judging.counts == {"verified": 0, "refuted": 2, "deferred": 1, "unverified": 0}
    assert verdict_from_facts(facts) is ReviewVerdict.REQUEST_CHANGES
    assert facts.open_blocking_findings == 1
    assert facts.open_blocking_ids == [mechanical_id]

    assert len(entries) == 4
    blocking = [entry for entry in entries if entry.blocking]
    assert len(blocking) == facts.open_blocking_findings
    assert [entry.finding_id for entry in blocking] == facts.open_blocking_ids
    listed_ids = [entry.finding_id for entry in entries]
    assert len(listed_ids) == len(set(listed_ids))
    for finding_id in facts.open_blocking_ids:
        assert listed_ids.count(finding_id) == 1


def test_report_names_each_judge_and_method(tmp_path: Path) -> None:
    _, mechanical_id, _, _, _, entries = _run(tmp_path)
    by_id = {entry.finding_id: entry for entry in entries}

    mechanical = by_id[mechanical_id]
    assert mechanical.judge == "mechanical"
    assert mechanical.state == "verified"
    assert mechanical.verification_method == "mechanical:missing_test"
    assert mechanical.evidence == "symbol_search: no test for test_migration_upgrade"

    refuter_states = sorted(entry.state for entry in entries if entry.judge == "refuter")
    assert refuter_states == ["deferred", "refuted", "refuted"]
    deferred = next(entry for entry in entries if entry.state == "deferred")
    assert deferred.evidence == "judging cap 2 reached"
    assert not deferred.blocking

    payload = mechanical.as_json()
    assert payload["judge"] == "mechanical"
    assert payload["verification_method"] == "mechanical:missing_test"
    assert payload["blocking"] is True
    assert payload["refutation"] is None
    assert deferred.as_json()["refutation"] == {
        "state": "deferred",
        "reason": "judging cap 2 reached",
        "evidence": [],
        "evidence_truncated": [],
        "evidence_tokens": 0,
    }


def test_a_replay_rebuilds_the_mechanical_entries_from_the_ledger(tmp_path: Path) -> None:
    db, _, _, judging, facts, fresh = _run(tmp_path)

    replayed = report_findings(
        db,
        pr_number=1,
        facts=facts,
        verifications=recorded_verifications(db, pr_number=1, head_sha=HEAD).settled,
        refutations=judging.outcomes,
    )

    assert [entry.as_json() for entry in replayed] == [entry.as_json() for entry in fresh]
    assert recorded_verifications(db, pr_number=1, head_sha="0" * 14).outcomes == ()


def test_a_blocking_finding_no_judge_named_is_still_listed(tmp_path: Path) -> None:
    db, mechanical_id, _, _, facts, _ = _run(tmp_path)

    entries = report_findings(db, pr_number=1, facts=facts)

    assert [entry.finding_id for entry in entries] == [mechanical_id]
    assert entries[0].judge == "mechanical"
    assert entries[0].verification_method == "mechanical:missing_test"
    assert entries[0].blocking


def test_the_authoritative_gate_names_the_same_ids(tmp_path: Path) -> None:
    db, mechanical_id, _, _, _, _ = _run(tmp_path)

    facts = gather_merge_facts(db, pr_number=1, repo_root=tmp_path, head_sha=HEAD, ci_green=True)

    assert facts.open_blocking_ids == [mechanical_id]
    assert len(facts.open_blocking_ids) == facts.open_blocking_findings


def test_stderr_table_and_sticky_report_carry_the_judge(tmp_path: Path) -> None:
    db, mechanical_id, _, _, facts, entries = _run(tmp_path)

    lines = findings_table_lines(entries, facts.open_blocking_ids)
    assert lines[0] == "report: 4 finding(s) listed"
    assert lines[-1] == f"verdict basis: #{mechanical_id}"
    mechanical_row = next(line for line in lines if line.startswith(f"  #{mechanical_id} "))
    assert "mechanical" in mechanical_row
    assert "mechanical:missing_test" in mechanical_row
    assert mechanical_row.endswith("BLOCKING")

    body = render_ledger_report(
        db,
        pr_number=1,
        decision=compute_merge_decision(facts),
        facts=facts,
        judged=entries,
    )
    assert "| Id | Judge | State | Method | Finding | Blocking |" in body
    assert f"Verdict basis: #{mechanical_id}" in body
    assert f"| {mechanical_id} | mechanical | verified | mechanical:missing_test |" in body


def test_team_outcome_archive_carries_findings_and_verdict_basis(tmp_path: Path) -> None:
    _, mechanical_id, _, _, facts, entries = _run(tmp_path)
    team = TeamOutcome(
        pr_number=1,
        final_verdict=verdict_from_facts(facts),
        n_blockers=0,
        n_majors=0,
        findings=entries,
        verdict_basis=list(facts.open_blocking_ids),
    )

    payload = team_outcome_to_dict(team)

    assert payload["verdict_basis"] == [mechanical_id]
    assert len(payload["findings"]) == 4
    assert {entry["judge"] for entry in payload["findings"]} == {"mechanical", "refuter"}


def test_an_uninitialised_ledger_still_lists_the_run_outcomes(tmp_path: Path) -> None:
    db = tmp_path / "never-created.db"
    facts = MergeFacts(
        head_sha=HEAD,
        ci_green=True,
        open_blocking_findings=0,
        spec_covered=False,
        spec_coverage_known=False,
        review_complete=True,
        review_integrity_known=False,
    )
    refutation = Refutation(7, "a.py", 3, ClaimType.DESIGN, "refuted", "vague")

    entries = report_findings(db, pr_number=1, facts=facts, refutations=[refutation])

    assert [(entry.finding_id, entry.judge, entry.state) for entry in entries] == [
        (7, "refuter", "refuted")
    ]
    assert entries[0].severity == ""
    assert entries[0].status == ""
    assert report_findings(db, pr_number=1, facts=facts) == []


def test_a_refuter_entry_carries_what_the_judge_was_served(tmp_path: Path) -> None:
    db = tmp_path / "never-created.db"
    facts = MergeFacts(
        head_sha=HEAD,
        ci_green=True,
        open_blocking_findings=0,
        spec_covered=False,
        spec_coverage_known=False,
        review_complete=True,
        review_integrity_known=False,
    )
    refutation = Refutation(
        7,
        "a.py",
        3,
        ClaimType.DESIGN,
        "deferred",
        "evidence outside window",
        evidence=("a.py:1-40 (whole file)",),
        detail="the caller is not served",
        evidence_truncated=("grep Foo: 60 hits (50 served)",),
        evidence_tokens=812,
    )

    (entry,) = report_findings(db, pr_number=1, facts=facts, refutations=[refutation])

    assert entry.as_json()["refutation"] == {
        "state": "deferred",
        "reason": "evidence outside window",
        "evidence": ["a.py:1-40 (whole file)"],
        "evidence_truncated": ["grep Foo: 60 hits (50 served)"],
        "evidence_tokens": 812,
        "detail": "the caller is not served",
    }
