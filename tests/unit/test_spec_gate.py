"""Unit tests for SP-SPEC-GATE — spec-coverage presence check.

Pins selector extraction/dedup, the data-only presence check (file +
node-id symbol), the coverage verdict (covered / partial / no
criteria), and the ledger round-trip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repoach.review.plan import ActionPlan, PlanStep, render_plan_markdown
from repoach.review.spec_gate import (
    BaseRefUnavailableError,
    acceptance_selectors,
    compute_spec_coverage,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    load_plan_from_ref,
    promised_body_non_trivial,
    promised_present,
    record_spec_coverage,
    resolve_contract_plan,
    selector_present,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_repo_on_develop(repo: Path) -> None:
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


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


def test_coverage_graded_against_base_plan(tmp_path: Path) -> None:
    """A PR that weakens its own plan is still graded against the base plan.

    ``develop`` commits the full contract (a unit test and an
    integration test, both present and real). The ``feature`` branch
    then WEAKENS ``docs/plans/SP-FOO.md`` — it drops the integration
    selector (and deletes the integration test file it no longer
    promises) while keeping the unit selector, still present and real.

    Graded against the WEAKENED head plan (the pre-fix behavior),
    every remaining promised selector is present, so coverage would
    read ``covered=True`` — the exploit this spec closes. Resolving
    the contract against ``base_ref="develop"`` must instead return
    the UNWEAKENED base plan, and grading THAT plan against the
    feature checkout must report the dropped selector as missing
    (SP-SPEC-CONTRACT-BASE G1 — the discriminating case that passes on
    the pre-fix head-loading code and must fail here).
    """
    _init_repo_on_develop(tmp_path)

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
    base_plan = _plan(
        unit_tests=["tests/unit/test_foo.py::test_foo"],
        integration_tests=["tests/integration/test_flow.py::test_flow"],
    )
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "SP-FOO.md").write_text(
        render_plan_markdown(base_plan), encoding="utf-8"
    )
    _commit_all(tmp_path, "docs(plan): full SP-FOO contract")

    _git(tmp_path, "checkout", "-q", "-b", "feature")
    weakened_plan = ActionPlan(
        spec_id="SP-FOO",
        title="Sample",
        summary="Weakened on head.",
        steps=[
            PlanStep(
                index=1,
                title="Keep only the unit test",
                files=["tests/unit/test_foo.py"],
                action="Drop the integration test promise.",
                commit_message="feat(foo): weaken",
                done_when="pytest tests/unit/test_foo.py is green",
                unit_tests=["tests/unit/test_foo.py::test_foo"],
            )
        ],
        integration_tests=[],
    )
    (tmp_path / "docs" / "plans" / "SP-FOO.md").write_text(
        render_plan_markdown(weakened_plan), encoding="utf-8"
    )
    (tmp_path / "tests" / "integration" / "test_flow.py").unlink()
    _commit_all(tmp_path, "feat(foo): weaken the plan and drop the test")

    exploited_coverage = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=weakened_plan)
    assert exploited_coverage.covered is True

    resolution = resolve_contract_plan("SP-FOO", repo_root=tmp_path, base_ref="develop")
    assert resolution.base_available is True
    assert resolution.fell_back_to_head is False
    assert resolution.plan == base_plan

    coverage = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=resolution.plan)
    assert coverage.covered is False
    assert coverage.missing == ["tests/integration/test_flow.py::test_flow"]


def test_coverage_graded_against_base_plan_falls_back_when_spec_is_new(tmp_path: Path) -> None:
    """A brand-new spec introduced only by the PR falls back to the head plan.

    ``develop`` carries no ``docs/plans/SP-NEW.md`` at all, so
    resolving the contract against it raises a bare "no such path"
    (git ``show`` failure), which :func:`resolve_contract_plan` treats
    as a first-introduction fallback (SP-SPEC-CONTRACT-BASE Edge
    cases) — never as a hard base-ref failure.
    """
    _init_repo_on_develop(tmp_path)
    (tmp_path / "README.md").write_text("root\n", encoding="utf-8")
    _commit_all(tmp_path, "chore: init develop")

    _git(tmp_path, "checkout", "-q", "-b", "feature")
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
    head_plan = _plan(
        unit_tests=["tests/unit/test_foo.py::test_foo"],
        integration_tests=["tests/integration/test_flow.py::test_flow"],
    )
    (tmp_path / "docs" / "plans").mkdir(parents=True)
    (tmp_path / "docs" / "plans" / "SP-FOO.md").write_text(
        render_plan_markdown(head_plan), encoding="utf-8"
    )
    _commit_all(tmp_path, "feat(foo): introduce SP-FOO")

    resolution = resolve_contract_plan("SP-FOO", repo_root=tmp_path, base_ref="develop")
    assert resolution.base_available is True
    assert resolution.fell_back_to_head is True
    assert resolution.plan == head_plan

    coverage = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=resolution.plan)
    assert coverage.covered is True


def test_coverage_fails_closed_when_base_ref_unavailable(tmp_path: Path) -> None:
    """An unresolvable base ref fails CLOSED, never falling back to the head plan.

    Grading against a branch that does not exist in the local clone
    (an unfetched base, or a fetch failure) must raise
    :class:`BaseRefUnavailableError` and :func:`resolve_contract_plan`
    must report ``base_available=False`` with no plan — the caller
    then records a NOT-covered report rather than silently trusting
    the head, which is exactly the attackable path this spec closes.
    """
    _init_repo_on_develop(tmp_path)
    (tmp_path / "README.md").write_text("root\n", encoding="utf-8")
    _commit_all(tmp_path, "chore: init")

    with pytest.raises(BaseRefUnavailableError):
        load_plan_from_ref("SP-FOO", "origin/develop", root=tmp_path)

    resolution = resolve_contract_plan("SP-FOO", repo_root=tmp_path, base_ref="origin/develop")
    assert resolution.base_available is False
    assert resolution.plan is None


def test_empty_body_promise_not_satisfied(tmp_path: Path) -> None:
    """A hollow ``pass`` / ``...`` promised test does not satisfy coverage.

    :func:`promised_body_non_trivial` rejects a trivial body (AC1) and
    :func:`compute_spec_coverage` composes it with :func:`selector_
    present`, so a PR that adds ``def test_hollow(): pass`` for a
    promised selector still shows up as missing even though the file
    and the symbol both exist at head (SP-SPEC-CONTRACT-BASE G3).
    """
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_trivial.py").write_text(
        "def test_pass():\n"
        "    pass\n"
        "\n"
        "\n"
        "def test_ellipsis():\n"
        "    ...\n"
        "\n"
        "\n"
        "def test_real():\n"
        "    assert 1 == 1\n",
        encoding="utf-8",
    )
    assert promised_body_non_trivial(tmp_path, "tests/test_trivial.py::test_pass") is False
    assert promised_body_non_trivial(tmp_path, "tests/test_trivial.py::test_ellipsis") is False
    assert promised_body_non_trivial(tmp_path, "tests/test_trivial.py::test_real") is True
    assert promised_body_non_trivial(tmp_path, "tests/test_trivial.py::test_absent") is False
    assert promised_body_non_trivial(tmp_path, "tests/test_trivial.py") is True

    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "src" / "repoach" / "foo.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_hollow.py").write_text(
        "def test_hollow():\n    pass\n", encoding="utf-8"
    )
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (tmp_path / "tests" / "integration" / "test_flow.py").write_text(
        "def test_flow():\n    assert True\n", encoding="utf-8"
    )
    plan = _plan(
        unit_tests=["tests/unit/test_hollow.py::test_hollow"],
        integration_tests=["tests/integration/test_flow.py::test_flow"],
    )
    coverage = compute_spec_coverage(tmp_path, spec_id="SP-FOO", plan=plan)
    assert coverage.covered is False
    assert coverage.missing == ["tests/unit/test_hollow.py::test_hollow"]
