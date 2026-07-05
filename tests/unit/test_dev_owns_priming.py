"""Tests for the Developer owns-priming brief (SP-ARCH-DEV-WIRE, slice B).

Covers the authoring `render_owns_brief` block and its injection into the
step brief via `build_step_brief`'s `arch_owns` parameter (backward-compat:
an empty arch_owns leaves the legacy brief unchanged).
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.dev_runner import build_step_brief
from ferova.review.governed_spec import (
    GovernedSpec,
    load_governed_spec,
    render_owns_brief,
)
from ferova.review.plan import ActionPlan, PlanStep


def _governed(spec_id: str, *, depends_on: str = "", owns_code: str = "src/x/") -> str:
    return (
        "---\n"
        f"id: {spec_id}\n"
        f"title: {spec_id}\n"
        "version: 0.1\n"
        "status: approved\n"
        "owns:\n"
        f"  code: [{owns_code}]\n"
        "  resources: N/A\n"
        f"depends_on: [{depends_on}]\n"
        "---\n\n# body\n"
    )


def _corpus(tmp_path: Path, specs: dict[str, str]) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    for name, text in specs.items():
        (specs_dir / name).write_text(text, encoding="utf-8")


def _plan() -> tuple[ActionPlan, PlanStep]:
    step = PlanStep(
        index=1,
        title="do the thing",
        action="implement",
        files=["src/x/mod.py", "tests/unit/test_x.py", "tests/integration/test_x.py"],
        commit_message="feat(x): do the thing",
        done_when="tests pass",
        unit_tests=["tests/unit/test_x.py::test_render_owns_brief_states_allowed_deps"],
    )
    plan = ActionPlan(
        spec_id="SP-X",
        title="X",
        summary="s",
        steps=[step],
        integration_tests=["tests/integration/test_x.py"],
    )
    return plan, step


def test_render_owns_brief_states_allowed_deps(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        {
            "01_SP-X_x.md": _governed("SP-X", depends_on="SP-ARCH-GRAPH"),
            "01_SP-ARCH-GRAPH_g.md": _governed("SP-ARCH-GRAPH", owns_code="src/arch/"),
        },
    )
    brief = render_owns_brief(load_governed_spec("SP-X", root=tmp_path))
    assert "Architecture contract" in brief
    assert "SP-ARCH-GRAPH" in brief
    assert "src/x/" in brief


def test_render_owns_brief_no_deps_warns(tmp_path: Path) -> None:
    _corpus(tmp_path, {"01_SP-ROOT_r.md": _governed("SP-ROOT")})
    brief = render_owns_brief(load_governed_spec("SP-ROOT", root=tmp_path))
    assert "NO dependencies" in brief


def test_render_owns_brief_empty_for_none() -> None:
    assert render_owns_brief(None) == ""


def test_build_step_brief_injects_arch_owns() -> None:
    plan, step = _plan()
    contract = render_owns_brief(
        GovernedSpec(id="SP-X", owns_code=("src/x/",), owns_resources=(), depends_on=("SP-DEP",))
    )
    brief = build_step_brief(plan, step, arch_owns=contract)
    assert "Architecture contract" in brief
    assert "SP-DEP" in brief


def test_build_step_brief_empty_arch_owns_is_legacy() -> None:
    plan, step = _plan()
    legacy = build_step_brief(plan, step)
    with_empty = build_step_brief(plan, step, arch_owns="")
    assert legacy == with_empty
    assert "Architecture contract" not in legacy
