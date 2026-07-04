"""SP-PLAN-CONTRACT-LINTS integration — the integration-promise lint fires on the committed-document path.

Complements the unit-level construction test in
``tests/unit/test_review_plan.py`` by proving the lint also fires when
a plan is rendered to Markdown and re-parsed — the path a committed
``docs/plans/<SP-ID>.md`` document actually travels through
(:func:`render_plan_markdown` then :func:`parse_plan_markdown`), not
just direct :class:`ActionPlan` construction.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from ferova.review.plan import ActionPlan, PlanStep, parse_plan_markdown, render_plan_markdown


def _violating_plan() -> ActionPlan:
    step = PlanStep(
        index=1,
        title="Add the feature",
        files=["src/ferova/feature.py", "tests/unit/test_feature.py"],
        action="Create the feature module with its unit test.",
        commit_message="feat(feature): add module",
        done_when="pytest tests/unit/test_feature.py is green",
        unit_tests=["tests/unit/test_feature.py"],
    )
    return ActionPlan(
        spec_id="SP-DEMO-LINT",
        title="Demo lint feature",
        summary="Ship the feature without ever creating its promised integration test.",
        steps=[step],
        integration_tests=["tests/integration/test_feature_e2e.py"],
    )


def test_integration_promise_lint_fires_on_round_trip() -> None:
    """A violating plan raises the same directive error after a render/parse round trip.

    A plan document is authored once, committed to disk as rendered
    Markdown, and re-read at ``ferova develop`` time via
    :func:`parse_plan_markdown`. This test proves the integration-promise
    lint fires on THAT path — not merely at direct construction — by
    first building an in-memory violating payload with
    ``ActionPlan.model_construct`` semantics: since :class:`ActionPlan`
    itself refuses to construct a violating instance, the round trip is
    exercised by rendering a compliant plan, mutating its promised
    integration test in the rendered text to reference a file no step
    creates, and asserting the reparse rejects it with the directive
    message.
    """
    step = PlanStep(
        index=1,
        title="Add the feature",
        files=["src/ferova/feature.py", "tests/unit/test_feature.py"],
        action="Create the feature module with its unit test.",
        commit_message="feat(feature): add module",
        done_when="pytest tests/unit/test_feature.py is green",
        unit_tests=["tests/unit/test_feature.py"],
    )
    compliant_plan = ActionPlan(
        spec_id="SP-DEMO-LINT",
        title="Demo lint feature",
        summary="Ship the feature; the integration test is created by the same step.",
        steps=[step],
        integration_tests=["tests/unit/test_feature.py"],
    )
    rendered = render_plan_markdown(compliant_plan)
    violating = rendered.replace(
        '"integration_tests": [\n    "tests/unit/test_feature.py"\n  ]',
        '"integration_tests": [\n    "tests/integration/test_feature_e2e.py"\n  ]',
        1,
    )

    with pytest.raises(ValidationError) as excinfo:
        parse_plan_markdown(violating)

    message = str(excinfo.value)
    assert "tests/integration/test_feature_e2e.py" in message
    assert "add that file" in message
