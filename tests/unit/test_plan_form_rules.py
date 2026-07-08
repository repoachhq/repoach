"""SP-PLAN-QUALITY step 1 — rule catalog covers every validator.

The Planner is given the whole rulebook instead of one error at a
time.  This module pins two contracts:

* every validator on :class:`PlanStep` and :class:`ActionPlan` has a
  one-line rule sentence in :data:`_FORM_RULES` (adding a validator
  without a sentence fails the catalog test);
* :func:`render_plan_form_rules` emits a stable, numbered, duplicate-
  free catalog suitable for injection into the Planner prompt.
"""

from __future__ import annotations

from ferova.review.plan import (
    _FORM_RULES,
    _STRICT_FORM_RULES,
    PLAN_STEP_MAX_FILES,
    PLAN_STEP_MAX_UNIT_SELECTORS,
    ActionPlan,
    PlanStep,
    render_plan_form_rules,
    validate_plan_form_strict,
)


def _registered_validator_names(model: type) -> set[str]:
    """Return the union of field- and model-validator registry names.

    Pydantic 2.13.4 keys ``__pydantic_decorators__.field_validators``
    and ``.model_validators`` by the decorated function's name, which
    matches the registry name the catalog uses.
    """
    decorators = model.__pydantic_decorators__
    return set(decorators.field_validators.keys()) | set(decorators.model_validators.keys())


class TestRuleCatalogCoversEveryValidator:
    def test_plan_step_validators_all_have_rule_sentences(self) -> None:
        for name in _registered_validator_names(PlanStep):
            assert name in _FORM_RULES, (
                f"PlanStep validator {name!r} has no rule sentence in _FORM_RULES"
            )

    def test_action_plan_validators_all_have_rule_sentences(self) -> None:
        for name in _registered_validator_names(ActionPlan):
            assert name in _FORM_RULES, (
                f"ActionPlan validator {name!r} has no rule sentence in _FORM_RULES"
            )

    def test_catalog_keys_are_unique(self) -> None:
        assert len(_FORM_RULES) == len(set(_FORM_RULES)), "_FORM_RULES has duplicate keys"


class TestCatalogRendersNumberedSentences:
    def test_rendered_text_contains_every_sentence(self) -> None:
        text = render_plan_form_rules()
        for sentence in _FORM_RULES.values():
            assert sentence in text, f"rendered catalog missing sentence: {sentence!r}"

    def test_rendered_text_is_numbered(self) -> None:
        text = render_plan_form_rules()
        for idx in range(1, len(_FORM_RULES) + 1):
            assert f"{idx}. " in text, f"rendered catalog missing entry {idx}"

    def test_rendered_text_has_no_duplicate_sentences(self) -> None:
        text = render_plan_form_rules()
        lines = [line for line in text.splitlines() if line.strip()]
        assert len(lines) == len(set(lines)), "rendered catalog has duplicate lines"

    def test_rendered_text_is_stable_across_calls(self) -> None:
        assert render_plan_form_rules() == render_plan_form_rules()


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


class TestStrictRulesRegisteredInCatalog:
    def test_strict_rules_are_present_and_rendered(self) -> None:
        assert len(_STRICT_FORM_RULES) >= 2
        text = render_plan_form_rules()
        for sentence in _STRICT_FORM_RULES.values():
            assert sentence in text, f"rendered catalog missing strict sentence: {sentence!r}"


class TestStepSizeCap:
    def test_step_size_cap_rejects_oversized_step(self) -> None:
        oversized_files_step = _step(
            index=1,
            files=[
                "src/ferova/a.py",
                "src/ferova/b.py",
                "src/ferova/c.py",
                "src/ferova/d.py",
                "tests/unit/test_demo.py",
                "tests/integration/test_demo_flow.py",
            ],
        )
        plan_files = _plan(steps=[oversized_files_step])
        reasons_files = validate_plan_form_strict(plan_files)
        assert any(
            str(PLAN_STEP_MAX_FILES) in reason and "30-turn" in reason for reason in reasons_files
        ), reasons_files

        oversized_selectors_step = _step(
            index=1,
            files=[
                "src/ferova/demo.py",
                "tests/unit/test_demo.py",
                "tests/integration/test_demo_flow.py",
            ],
            unit_tests=[f"tests/unit/test_demo.py::test_{i}" for i in range(6)],
        )
        plan_selectors = _plan(steps=[oversized_selectors_step])
        reasons_selectors = validate_plan_form_strict(plan_selectors)
        assert any(
            str(PLAN_STEP_MAX_UNIT_SELECTORS) in reason and "30-turn" in reason
            for reason in reasons_selectors
        ), reasons_selectors


class TestNoStubDoubleLint:
    def test_form_lint_rejects_banned_double_keywords(self) -> None:
        stubbing_step = _step(
            index=1,
            action="Monkeypatch resolve_verified_head to return a fixed sha.",
        )
        plan = _plan(steps=[stubbing_step])
        reasons = validate_plan_form_strict(plan)
        assert any("operator rule" in reason for reason in reasons), reasons

        substring_step = _step(
            index=1,
            action="Rename the mockingbird_helper identifier to a clearer name.",
        )
        plan_substring = _plan(steps=[substring_step])
        assert validate_plan_form_strict(plan_substring) == []

        prose_step = _step(
            index=1,
            action="The legacy client was stubborn about retries; simplify it.",
        )
        plan_prose = _plan(steps=[prose_step])
        assert validate_plan_form_strict(plan_prose) == []

    def test_form_lint_allows_truthful_boundary_fakes(self) -> None:
        truthful_fake_step = _step(
            index=1,
            action=("Add a truthful gh boundary fake whose pr_head_sha is scripted by the test."),
        )
        plan = _plan(steps=[truthful_fake_step])
        assert validate_plan_form_strict(plan) == []
