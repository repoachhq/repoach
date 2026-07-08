"""SP-PLAN-FORM — plan model validation, render and parse round-trip.

Covers the Definition of Done matrix: a fully valid plan, every
lexical path rejection, both layers of the test contract (per-step
unit tests, per-plan integration tests), index contiguity, spec-id
shape, renderer layout, parser failure modes and the round-trip law
``parse_plan_markdown(render_plan_markdown(p)) == p``.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ferova.review.plan import (
    PLAN_MARKER,
    ActionPlan,
    PlanStep,
    load_plan,
    parse_plan_markdown,
    plan_relpath,
    render_plan_markdown,
)


def _step(**overrides) -> PlanStep:
    payload = {
        "index": 1,
        "title": "Add the module",
        "files": [
            "src/ferova/demo.py",
            "tests/unit/test_demo.py",
            "tests/integration/test_demo_flow.py",
        ],
        "action": "Create the module with the documented API.",
        "commit_message": "feat(demo): add module",
        "done_when": "pytest tests/unit/test_demo.py is green",
        "unit_tests": ["tests/unit/test_demo.py::test_new_thing"],
    }
    payload.update(overrides)
    return PlanStep(**payload)


def _plan(**overrides) -> ActionPlan:
    payload = {
        "spec_id": "SP-DEMO",
        "title": "Demo feature",
        "summary": "Ship the demo module end to end.",
        "steps": [_step()],
        "integration_tests": ["tests/integration/test_demo_flow.py"],
    }
    payload.update(overrides)
    return ActionPlan(**payload)


class TestPlanStepValidation:
    def test_fully_valid_step_accepted(self) -> None:
        step = _step()
        assert step.index == 1
        assert step.unit_tests == ["tests/unit/test_demo.py::test_new_thing"]

    def test_absolute_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="absolute"):
            _step(files=["/etc/passwd"])

    def test_traversal_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match=r"\.\."):
            _step(files=["src/../../outside.py"])

    def test_empty_path_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _step(files=["  "])

    def test_no_files_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one file"):
            _step(files=[])

    def test_code_step_without_unit_tests_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must promise at least one unit test"):
            _step(unit_tests=[])

    def test_docs_only_step_without_unit_tests_accepted(self) -> None:
        step = _step(files=["docs/plans/SP-DEMO.md"], unit_tests=[])
        assert step.unit_tests == []

    def test_flag_like_unit_test_selector_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not start with '-'"):
            _step(unit_tests=["--pdb"])

    def test_blank_unit_test_selector_rejected(self) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _step(unit_tests=["   "])

    @pytest.mark.parametrize("field", ["title", "action", "commit_message", "done_when"])
    def test_blank_required_text_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _step(**{field: "   "})

    def test_bare_file_unit_promise_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="promise the exact test function"):
            _step(unit_tests=["tests/unit/test_demo.py"])

    def test_node_id_unit_promise_is_accepted(self) -> None:
        step = _step(unit_tests=["tests/unit/test_demo.py::test_new_thing"])
        assert step.unit_tests == ["tests/unit/test_demo.py::test_new_thing"]


class TestActionPlanValidation:
    def test_fully_valid_plan_accepted(self) -> None:
        plan = _plan()
        assert plan.spec_id == "SP-DEMO"
        assert len(plan.steps) == 1

    def test_bad_spec_id_rejected(self) -> None:
        with pytest.raises(ValidationError, match="SP-"):
            _plan(spec_id="sp-demo")

    def test_empty_steps_rejected(self) -> None:
        with pytest.raises(ValidationError, match="at least one step"):
            _plan(steps=[])

    def test_non_contiguous_indexes_rejected(self) -> None:
        with pytest.raises(ValidationError, match="indexes must be exactly"):
            _plan(steps=[_step(index=1), _step(index=3)])

    def test_duplicate_indexes_rejected(self) -> None:
        with pytest.raises(ValidationError, match="indexes must be exactly"):
            _plan(steps=[_step(index=1), _step(index=1)])

    def test_src_plan_without_integration_tests_rejected(self) -> None:
        with pytest.raises(ValidationError, match="integration test"):
            _plan(integration_tests=[])

    def test_docs_only_plan_with_empty_integration_promises_stays_valid(self) -> None:
        docs_step = _step(files=["docs/notes.md"], unit_tests=[])
        plan = _plan(steps=[docs_step], integration_tests=[])
        assert plan.integration_tests == []

    def test_flag_like_integration_test_selector_rejected(self) -> None:
        with pytest.raises(ValidationError, match="must not start with '-'"):
            _plan(integration_tests=["-p evil_plugin"])

    def test_forward_test_reference_rejected(self) -> None:
        code_step = _step(
            index=1,
            files=["src/ferova/feature.py"],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        test_step = _step(
            index=2,
            files=["tests/unit/test_feature.py"],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        with pytest.raises(ValidationError, match="no step up to 1 creates"):
            _plan(steps=[code_step, test_step])

    def test_promised_test_created_nowhere_rejected(self) -> None:
        step = _step(
            index=1,
            files=["src/ferova/feature.py"],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        with pytest.raises(ValidationError, match="no step up to 1 creates"):
            _plan(steps=[step])

    def test_same_step_code_and_test_accepted(self) -> None:
        step = _step(
            index=1,
            files=[
                "src/ferova/feature.py",
                "tests/unit/test_feature.py",
                "tests/integration/test_demo_flow.py",
            ],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        plan = _plan(steps=[step])
        assert plan.steps[0].unit_tests == ["tests/unit/test_feature.py::test_it"]

    def test_test_created_by_earlier_step_accepted(self) -> None:
        first = _step(
            index=1,
            files=[
                "src/a.py",
                "tests/unit/test_shared.py",
                "tests/integration/test_demo_flow.py",
            ],
            unit_tests=["tests/unit/test_shared.py::test_a"],
        )
        second = _step(
            index=2,
            files=["src/b.py", "tests/integration/test_demo_flow.py"],
            unit_tests=["tests/unit/test_shared.py::test_b"],
        )
        plan = _plan(steps=[first, second])
        assert len(plan.steps) == 2

    @pytest.mark.parametrize("field", ["title", "summary"])
    def test_blank_required_text_rejected(self, field: str) -> None:
        with pytest.raises(ValidationError, match="non-empty"):
            _plan(**{field: ""})

    def test_integration_promise_without_creating_step_is_rejected(self) -> None:
        step = _step(
            index=1,
            files=["src/ferova/feature.py", "tests/unit/test_feature.py"],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        with pytest.raises(ValidationError) as excinfo:
            _plan(steps=[step], integration_tests=["tests/integration/test_feature_e2e.py"])
        message = str(excinfo.value)
        assert "tests/integration/test_feature_e2e.py" in message
        assert "add that file" in message

    def test_integration_promise_created_by_any_step_is_accepted(self) -> None:
        first = _step(
            index=1,
            files=["src/ferova/feature.py", "tests/unit/test_feature.py"],
            unit_tests=["tests/unit/test_feature.py::test_it"],
        )
        second = _step(
            index=2,
            files=["tests/integration/test_feature_e2e.py"],
            unit_tests=["tests/unit/test_feature.py::test_smoke"],
        )
        plan = _plan(
            steps=[first, second],
            integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"],
        )
        assert plan.integration_tests == ["tests/integration/test_feature_e2e.py::test_e2e"]

    def test_integration_promise_node_id_resolves_file_part(self) -> None:
        step = _step(
            index=1,
            files=["src/ferova/feature.py", "tests/integration/test_feature_e2e.py"],
            unit_tests=["tests/integration/test_feature_e2e.py::test_smoke"],
        )
        plan = _plan(
            steps=[step],
            integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"],
        )
        assert plan.integration_tests == ["tests/integration/test_feature_e2e.py::test_e2e"]


class TestRender:
    def test_render_contains_marker_fence_and_step_titles(self) -> None:
        two_step = _plan(
            steps=[_step(), _step(index=2, title="Wire the CLI")],
        )
        text = render_plan_markdown(two_step)
        assert text.startswith("# SP-DEMO — Demo feature")
        assert "## Step 1 — Add the module" in text
        assert "## Step 2 — Wire the CLI" in text
        assert PLAN_MARKER in text
        assert text.index(PLAN_MARKER) < text.index("```json")
        assert "tests/integration/test_demo_flow.py" in text

    def test_json_fence_is_the_last_fence(self) -> None:
        text = render_plan_markdown(_plan())
        assert text.rstrip().endswith("```")
        assert text.count("```json") == 1

    def test_docs_only_step_renders_exemption_note(self) -> None:
        docs_step = _step(files=["docs/notes.md"], unit_tests=[])
        text = render_plan_markdown(_plan(steps=[docs_step], integration_tests=[]))
        assert "docs-only step" in text
        assert "_(none promised)_" in text


class TestParseRoundTrip:
    def test_round_trip_equality(self) -> None:
        plan = _plan(
            steps=[
                _step(),
                _step(
                    index=2,
                    title="Document it",
                    files=["docs/plans/SP-DEMO.md"],
                    unit_tests=[],
                ),
            ],
        )
        assert parse_plan_markdown(render_plan_markdown(plan)) == plan

    def test_missing_marker_raises(self) -> None:
        with pytest.raises(ValueError, match="marker"):
            parse_plan_markdown("# A document without the canonical payload\n")

    def test_marker_without_fence_raises(self) -> None:
        with pytest.raises(ValueError, match="no json fence"):
            parse_plan_markdown(f"# Doc\n\n{PLAN_MARKER}\n\nno fence here\n")

    def test_invalid_payload_raises_validation_error(self) -> None:
        bad = f'{PLAN_MARKER}\n```json\n{{"spec_id": "nope"}}\n```\n'
        with pytest.raises(ValueError):
            parse_plan_markdown(bad)


class TestLoadPlan:
    def test_plan_relpath_shape(self) -> None:
        assert plan_relpath("SP-PLAN-FORM") == "docs/plans/SP-PLAN-FORM.md"

    def test_load_plan_happy_path(self, tmp_path) -> None:
        plan = _plan()
        target = tmp_path / plan_relpath(plan.spec_id)
        target.parent.mkdir(parents=True)
        target.write_text(render_plan_markdown(plan), encoding="utf-8")
        assert load_plan(plan.spec_id, root=tmp_path) == plan

    def test_load_plan_missing_file_raises(self, tmp_path) -> None:
        with pytest.raises(FileNotFoundError, match="SP-ABSENT"):
            load_plan("SP-ABSENT", root=tmp_path)


class TestNodeIdAndIntegrationTreeLints:
    def test_unit_path_integration_promise_is_rejected(self) -> None:
        step = _step(
            files=[
                "src/ferova/demo.py",
                "tests/unit/test_demo.py",
            ],
            unit_tests=["tests/unit/test_demo.py::test_new_thing"],
        )
        with pytest.raises(ValidationError, match=r"tests/unit/test_demo\.py"):
            _plan(steps=[step], integration_tests=["tests/unit/test_demo.py"])

    def test_integration_tree_promise_is_accepted(self) -> None:
        step = _step(
            files=[
                "src/ferova/demo.py",
                "tests/unit/test_demo.py",
                "tests/integration/test_x.py",
            ],
            unit_tests=["tests/unit/test_demo.py::test_new_thing"],
        )
        plan = _plan(
            steps=[step],
            integration_tests=["tests/integration/test_x.py::test_e2e"],
        )
        assert plan.integration_tests == ["tests/integration/test_x.py::test_e2e"]
