"""Integration test for build_step_brief contract-file embedding (SP-DEV-BRIEF-FILE-CONTENT).

Exercises build_step_brief against a real on-disk step contract: one existing
source file and one new test file. Asserts the brief carries the source content
and a to-create entry for the test.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.dev_runner import build_step_brief
from ferova.review.plan import ActionPlan, PlanStep


def _one_step_plan(files: list[str]) -> tuple[ActionPlan, PlanStep]:
    """Return a minimal one-step plan whose step's contract is *files*.

    The promised unit test file is always included in *files* so the
    plan-form validator accepts it.  When *files* touches ``src/`` an
    integration test promise is added to satisfy the src-interlock,
    and the integration test file is added to the step's contract so
    the validator's "promised but no step creates" check passes.
    """
    touches_src = any(f.startswith("src/") for f in files)
    all_files = list(files)
    test_file = "tests/unit/test_foo_new.py"
    if test_file not in all_files:
        all_files.append(test_file)
    integration_test_file = "tests/integration/test_foo_flow.py"
    if touches_src and integration_test_file not in all_files:
        all_files.append(integration_test_file)
    step = PlanStep(
        index=1,
        title="do the thing",
        action="implement it",
        files=all_files,
        commit_message="feat(foo): do the thing",
        done_when="tests pass",
        unit_tests=[f"{test_file}::test_something"],
    )
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="Foo",
        summary="s",
        steps=[step],
        integration_tests=[integration_test_file] if touches_src else [],
    )
    return plan, step


def test_build_step_brief_embeds_source_and_lists_to_create(tmp_path: Path, monkeypatch) -> None:
    """End-to-end: one existing source file, one new test file.

    The brief must carry the source file's content under a heading and
    list the test file under 'Files to create'.
    """
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("def foo():\n    return 42\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, step = _one_step_plan(["src/ferova/foo.py", "tests/unit/test_foo_new.py"])

    brief = build_step_brief(plan, step)

    assert "## Contract files" in brief
    assert "### `src/ferova/foo.py`" in brief
    assert "def foo():" in brief
    assert "return 42" in brief
    assert "## Files to create" in brief
    assert "- `tests/unit/test_foo_new.py`" in brief
