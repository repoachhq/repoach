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
    ActionPlan,
    PlanStep,
    render_plan_form_rules,
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
