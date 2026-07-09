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
    _BANNED_DOUBLE_KEYWORDS,
    _FORM_RULES,
    _STRICT_FORM_RULES,
    PLAN_STEP_MAX_ACTION_DENSITY,
    PLAN_STEP_MAX_FILES,
    PLAN_STEP_MAX_UNIT_SELECTORS,
    ActionPlan,
    PlanStep,
    parse_plan_markdown,
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


class TestActionDensityCap:
    def test_action_density_cap_rejects_dense_step(self) -> None:
        long_action = "x" * (2 * PLAN_STEP_MAX_ACTION_DENSITY + 100)
        dense_step = _step(
            index=1,
            files=["tests/unit/test_a.py", "tests/unit/test_b.py"],
            action=long_action,
            unit_tests=["tests/unit/test_a.py::test_foo"],
        )
        plan_dense = _plan(steps=[dense_step], integration_tests=[])
        reasons = validate_plan_form_strict(plan_dense)
        assert any(
            str(PLAN_STEP_MAX_ACTION_DENSITY) in reason and "30-turn" in reason
            for reason in reasons
        ), reasons

        spread_files = [
            "tests/unit/test_a.py",
            "tests/unit/test_b.py",
            "tests/unit/test_c.py",
        ]
        spread_step = _step(
            index=1,
            files=spread_files,
            action=long_action,
            unit_tests=["tests/unit/test_a.py::test_foo"],
        )
        plan_spread = _plan(steps=[spread_step], integration_tests=[])
        assert validate_plan_form_strict(plan_spread) == []

    def test_action_density_cap_is_emission_only(self) -> None:
        long_action = "x" * (2 * PLAN_STEP_MAX_ACTION_DENSITY + 100)
        dense_step = _step(
            index=1,
            files=["tests/unit/test_a.py", "tests/unit/test_b.py"],
            action=long_action,
            unit_tests=["tests/unit/test_a.py::test_foo"],
        )
        plan = _plan(steps=[dense_step], integration_tests=[])

        reasons = validate_plan_form_strict(plan)
        assert any(str(PLAN_STEP_MAX_ACTION_DENSITY) in reason for reason in reasons), (
            "strict layer must flag the dense step"
        )

        rendered = plan.model_dump_json(indent=2)
        parsed = ActionPlan.model_validate_json(rendered)
        assert parsed == plan

        markdown = (
            f"# SP-DEMO \u2014 Demo feature\n\n"
            f"Ship the demo module end to end.\n\n"
            f"## Step 1 \u2014 Add the module\n\n"
            f"- **Files**: `tests/unit/test_a.py`, `tests/unit/test_b.py`\n"
            f"- **Action**: {long_action}\n"
            f"- **Commit**: `feat(demo): add module`\n"
            f"- **Done when**: `pytest tests/unit/test_demo.py is green`\n"
            f"- **Unit tests**: `tests/unit/test_a.py::test_foo`\n\n"
            f"## Integration tests\n\n"
            f"_(none promised)_\n\n"
            f"<!-- ferova-action-plan -->\n"
            f"```json\n{rendered}\n```\n"
        )
        round_tripped = parse_plan_markdown(markdown)
        assert round_tripped == plan


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


def test_rule_catalog_covers_every_validator() -> None:
    """Every registered validator name on both models has a rule sentence."""
    names = _registered_validator_names(PlanStep) | _registered_validator_names(ActionPlan)
    missing = names - set(_FORM_RULES)
    assert not missing, f"validators without a rule sentence: {sorted(missing)}"


def test_catalog_renders_numbered_sentences() -> None:
    """The rendered catalog numbers every sentence exactly once."""
    rendered = render_plan_form_rules()
    sentences = list(_FORM_RULES.values()) + list(_STRICT_FORM_RULES.values())
    for sentence in sentences:
        assert sentence in rendered
    lines = [line for line in rendered.splitlines() if line.strip()]
    numbered = [line for line in lines if line.lstrip()[0].isdigit()]
    assert len(numbered) == len(set(numbered)) >= len(set(sentences))


def test_banned_keyword_set_matches_spec() -> None:
    """The banned test-double keyword set matches spec G3 verbatim.

    G3 enumerates exactly seven words: the noun for a canned double
    (``stub``) and its two participle forms (``stubbed``,
    ``stubbing``), the pytest fixture-patching verb (``monkeypatch``),
    and the three forms of the other test-double noun (``mock``,
    ``mocked``, ``mocking``). Each must trip the lint as a whole word;
    substrings inside identifiers or unrelated words (``stubborn``,
    ``mockingbird``) must not.
    """
    spec_words = frozenset(
        {"stub", "stubbed", "stubbing", "monkeypatch", "mock", "mocked", "mocking"}
    )
    assert spec_words == _BANNED_DOUBLE_KEYWORDS

    for word in sorted(spec_words):
        step = _step(index=1, action=f"Please {word} the target behavior in this step.")
        plan = _plan(steps=[step])
        reasons = validate_plan_form_strict(plan)
        assert reasons, f"expected a reason for banned keyword {word!r}"

    for safe_text in (
        "The legacy client was stubborn about retries; simplify it.",
        "Rename the mockingbird_helper identifier to a clearer name.",
    ):
        step = _step(index=1, action=safe_text)
        plan = _plan(steps=[step])
        assert validate_plan_form_strict(plan) == [], safe_text
