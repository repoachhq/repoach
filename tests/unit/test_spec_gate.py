"""Unit tests for SP-SPEC-GATE — spec-coverage presence check.

Pins selector extraction/dedup, the data-only presence check (file +
node-id symbol), the coverage verdict (covered / partial / no
criteria), and the ledger round-trip.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec_gate import (
    acceptance_selectors,
    compute_spec_coverage,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    promised_present,
    record_spec_coverage,
    selector_present,
)


def _plan(*, unit_tests: list[str], integration_tests: list[str]) -> ActionPlan:
    promised_files = [t.split("::", 1)[0] for t in [*unit_tests, *integration_tests]]
    step = PlanStep(
        index=1,
        title="Do the step",
        files=["src/repoach/foo.py", *promised_files],
        action="Implement.",
        commit_message="feat(foo): step",
        done_when="gates green",
        unit_tests=unit_tests,
    )
    return ActionPlan(
        spec_id="SP-FOO",
        title="Sample",
        summary="One step.",
        steps=[step],
        integration_tests=integration_tests,
    )


def test_acceptance_selectors_dedup_in_order() -> None:
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="t",
        summary="s",
        steps=[
            PlanStep(
                index=1,
                title="a",
                files=["src/x.py", "tests/unit/test_a.py", "tests/integration/test_flow.py"],
                action="x",
                commit_message="feat(x): a",
                done_when="ok",
                unit_tests=["tests/unit/test_a.py::test_one", "tests/unit/test_a.py::test_one"],
            ),
        ],
        integration_tests=["tests/integration/test_flow.py"],
    )
    assert acceptance_selectors(plan) == [
        "tests/unit/test_a.py::test_one",
        "tests/integration/test_flow.py",
    ]


def test_selector_present_file_and_symbol(tmp_path: Path) -> None:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_a.py").write_text(
        "def test_one():\n    assert True\n", encoding="utf-8"
    )
    assert selector_present(tmp_path, "tests/unit/test_a.py::test_one") is True
    assert selector_present(tmp_path, "tests/unit/test_a.py") is True
    assert selector_present(tmp_path, "tests/unit/test_a.py::test_absent") is False
    assert selector_present(tmp_path, "tests/unit/test_ghost.py::test_one") is False


def test_selector_present_resolves_class_scoped_node_ids(tmp_path: Path) -> None:
    """A ``file::TestClass::test_name`` selector resolves the trailing name.

    The trailing-name match is class-nesting tolerant: a class-scoped
    promise is satisfied by any ``def <trailing_name>(`` in the file,
    regardless of intermediate class segments
    (SP-DEV-PROMISE-TRAILING-NAME, 2026-07-10). The whole node was
    previously matched as one function name, so every class-scoped
    promised selector failed self-verify and spec coverage even while
    pytest ran it green (SP-DEV-STEP-PREFLIGHT, 2026-07-04: five green
    predicate tests reported absent at head).
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_c.py").write_text(
        "class TestThing:\n    def test_inner(self):\n        assert True\n",
        encoding="utf-8",
    )
    assert selector_present(tmp_path, "tests/test_c.py::TestThing::test_inner") is True
    assert selector_present(tmp_path, "tests/test_c.py::TestThing::test_absent") is False
    assert selector_present(tmp_path, "tests/test_c.py::TestGhost::test_inner") is True
    assert selector_present(tmp_path, "tests/test_c.py::TestThing::test_inner[p0]") is True


def test_selector_present_strips_parametrize_id(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_p.py").write_text(
        "def test_param():\n    assert True\n", encoding="utf-8"
    )
    assert selector_present(tmp_path, "tests/test_p.py::test_param[case1]") is True


def test_promised_present_matches_class_nested_method(tmp_path: Path) -> None:
    """A class-nested method satisfies both flat and class-scoped promises.

    The trailing-name match is class-nesting tolerant: a promise
    ``path::test_foo`` OR ``path::TestBaz::test_foo`` is satisfied by
    any ``def test_foo(`` in the file, regardless of intermediate
    class segments (SP-DEV-PROMISE-TRAILING-NAME, 2026-07-10).
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_nested.py").write_text(
        "class TestBar:\n    def test_foo(self):\n        assert True\n",
        encoding="utf-8",
    )
    assert promised_present(tmp_path, "tests/test_nested.py::test_foo") is True
    assert promised_present(tmp_path, "tests/test_nested.py::TestBaz::test_foo") is True


def test_promised_present_word_boundary(tmp_path: Path) -> None:
    """A word-boundary regex rejects substring matches and missing defs.

    The previous substring scan ``f"def {name}" in source`` wrongly
    satisfied promise ``test_foo`` with ``def test_foobar(``; the new
    regex ``def\\s+NAME\\s*\\(`` requires the trailing ``(`` so the
    prefix is not a match (SP-DEV-PROMISE-TRAILING-NAME, 2026-07-10).
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_prefix.py").write_text(
        "def test_foobar(self):\n    assert True\n",
        encoding="utf-8",
    )
    assert promised_present(tmp_path, "tests/test_prefix.py::test_foo") is False

    (tmp_path / "tests" / "test_empty.py").write_text("x = 1\n", encoding="utf-8")
    assert promised_present(tmp_path, "tests/test_empty.py::test_foo") is False


def test_compute_coverage_fully_covered(tmp_path: Path) -> None:
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "src" / "repoach" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_foo.py").write_text(
        "def test_foo():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_flow.py").write_text(
        "def test_flow():\n    assert True\n", encoding="utf-8"
    )
    plan = _plan(
        unit_tests=["tests/unit/test_foo.py::test_foo"],
        integration_tests=["tests/integration/test_flow.py::test_flow"],
    )
    cov = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=plan)
    assert cov.covered is True
    assert cov.n_promised == 2
    assert cov.n_present == 2
    assert cov.missing == []


def test_compute_coverage_partial_when_promised_test_absent(tmp_path: Path) -> None:
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "src" / "repoach" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_foo.py").write_text(
        "def test_foo():\n    assert True\n", encoding="utf-8"
    )
    plan = _plan(
        unit_tests=["tests/unit/test_foo.py::test_foo"],
        integration_tests=["tests/integration/test_missing.py::test_gone"],
    )
    cov = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=plan)
    assert cov.covered is False
    assert cov.missing == ["tests/integration/test_missing.py::test_gone"]


def test_coverage_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "c.db"
    init_spec_coverage_schema(db)
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "src" / "repoach" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_foo.py").write_text(
        "def test_foo():\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_flow.py").write_text(
        "def test_flow():\n    assert True\n", encoding="utf-8"
    )
    plan = _plan(
        unit_tests=["tests/unit/test_foo.py::test_foo"],
        integration_tests=["tests/integration/test_flow.py::test_flow"],
    )
    cov = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=plan)
    record_spec_coverage(db, pr_number=42, head_sha="head123", coverage=cov)

    rows = fetch_spec_coverage(db, 42)
    assert len(rows) == 1
    assert rows[0].covered is True
    assert rows[0].spec_id == "SP-FOO"
    assert rows[0].n_promised == 2
