"""Integration test for SP-GATE-ASYNC-DEF-SELECTOR — async def tests satisfy
the merge gate end-to-end.

Proves G4 of the spec: a plan promising async selectors over a head
that delivers them yields ``SpecCoverage.covered == True``, and the
merge decision carries no "spec acceptance selectors not all present"
reason.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.findings import init_findings_schema, record_review_integrity
from repoach.review.merge_gate import compute_merge_decision, gather_merge_facts
from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec_gate import compute_spec_coverage, record_spec_coverage


def _plan_with_async_selectors(unit_tests: list[str], integration_tests: list[str]) -> ActionPlan:
    """Build a minimal plan whose step files cover every promised selector."""
    promised_files = [t.split("::", 1)[0] for t in [*unit_tests, *integration_tests]]
    step = PlanStep(
        index=1,
        title="Deliver async def tests",
        files=["src/repoach/foo.py", *promised_files],
        action="Implement async test delivery.",
        commit_message="feat(async-tests): deliver",
        done_when="gates green",
        unit_tests=unit_tests,
    )
    return ActionPlan(
        spec_id="SP-GATE-ASYNC-DEF",
        title="Async def test delivery",
        summary="Deliver only async def test functions.",
        steps=[step],
        integration_tests=integration_tests,
    )


def test_async_promises_yield_covered_and_gate_reason_free(tmp_path: Path) -> None:
    """Build a head with only async def tests, compute coverage, record
    it, gather merge facts, and assert the gate is clean."""
    repo = tmp_path / "repo"
    (repo / "src" / "repoach").mkdir(parents=True)
    (repo / "src" / "repoach" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (repo / "tests" / "unit").mkdir(parents=True)
    (repo / "tests" / "integration").mkdir(parents=True)

    unit_async = (
        "async def test_async_one():\n    assert True\n\n"
        "async def test_async_two():\n    assert True\n"
    )
    (repo / "tests" / "unit" / "test_async_units.py").write_text(unit_async, encoding="utf-8")
    int_async = "async def test_integration_flow():\n    assert True\n"
    (repo / "tests" / "integration" / "test_async_int.py").write_text(int_async, encoding="utf-8")

    plan = _plan_with_async_selectors(
        unit_tests=[
            "tests/unit/test_async_units.py::test_async_one",
            "tests/unit/test_async_units.py::test_async_two",
        ],
        integration_tests=["tests/integration/test_async_int.py::test_integration_flow"],
    )

    cov = compute_spec_coverage(repo, spec_id="SP-GATE-ASYNC-DEF", plan=plan)
    assert cov.covered is True
    assert cov.missing == []
    assert cov.n_promised == 3
    assert cov.n_present == 3

    db = tmp_path / "gate.db"
    init_findings_schema(db)
    record_spec_coverage(
        db,
        pr_number=1,
        head_sha="abc123",
        coverage=cov,
    )
    record_review_integrity(db, pr_number=1, head_sha="abc123", n_reviewers=4, n_unparsed=0)

    facts = gather_merge_facts(db, pr_number=1, repo_root=repo, head_sha="abc123", ci_green=True)
    decision = compute_merge_decision(facts)
    assert decision.merge is True
    assert not any("spec acceptance selectors not all present" in r for r in decision.reasons)
