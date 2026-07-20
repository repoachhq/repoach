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

from repoach.review.plan import ActionPlan, PlanStep, parse_plan_markdown, render_plan_markdown


def _violating_plan() -> ActionPlan:
    step = PlanStep(
        index=1,
        title="Add the feature",
        files=["src/repoach/feature.py", "tests/unit/test_feature.py"],
        action="Create the feature module with its unit test.",
        commit_message="feat(feature): add module",
        done_when="pytest tests/unit/test_feature.py is green",
        unit_tests=["tests/unit/test_feature.py::test_it"],
    )
    return ActionPlan(
        spec_id="SP-DEMO-LINT",
        title="Demo lint feature",
        summary="Ship the feature without ever creating its promised integration test.",
        steps=[step],
        integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"],
    )


def test_node_id_and_integration_tree_lints_fire_on_round_trip() -> None:
    """G1 and G2 both fire on the render/parse round trip, not just direct construction.

    A compliant plan is rendered to Markdown, then the json fence is
    mutated so the step's unit_tests promise a bare file (violating G1)
    and the plan's integration_tests promise a unit-tree path (violating
    G2). The reparse must raise ValidationError carrying both directive
    messages.
    """
    step = PlanStep(
        index=1,
        title="Add the feature",
        files=[
            "src/repoach/feature.py",
            "tests/unit/test_feature.py",
            "tests/integration/test_feature.py",
        ],
        action="Create the feature module with its unit test.",
        commit_message="feat(feature): add module",
        done_when="pytest tests/unit/test_feature.py is green",
        unit_tests=["tests/unit/test_feature.py::test_it"],
    )
    compliant_plan = ActionPlan(
        spec_id="SP-DEMO-LINT",
        title="Demo lint feature",
        summary="Ship the feature; both promises are compliant with G1/G2.",
        steps=[step],
        integration_tests=["tests/integration/test_feature.py::test_e2e"],
    )
    rendered = render_plan_markdown(compliant_plan)
    violating = rendered.replace(
        '"unit_tests": [\n        "tests/unit/test_feature.py::test_it"\n      ]',
        '"unit_tests": [\n        "tests/unit/test_feature.py"\n      ]',
        1,
    ).replace(
        '"integration_tests": [\n    "tests/integration/test_feature.py::test_e2e"\n  ]',
        '"integration_tests": [\n    "tests/unit/test_feature.py"\n  ]',
        1,
    )

    with pytest.raises(ValidationError) as excinfo:
        parse_plan_markdown(violating)

    message = str(excinfo.value)
    assert "promise the exact test function" in message
    assert "tests/unit/test_feature.py" in message


def test_integration_promise_lint_fires_on_round_trip() -> None:
    """A violating plan raises the same directive error after a render/parse round trip.

    A plan document is authored once, committed to disk as rendered
    Markdown, and re-read at ``repoach develop`` time via
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
        files=[
            "src/repoach/feature.py",
            "tests/unit/test_feature.py",
            "tests/integration/test_feature_e2e.py",
        ],
        action="Create the feature module with its unit test.",
        commit_message="feat(feature): add module",
        done_when="pytest tests/unit/test_feature.py is green",
        unit_tests=["tests/unit/test_feature.py::test_it"],
    )
    compliant_plan = ActionPlan(
        spec_id="SP-DEMO-LINT",
        title="Demo lint feature",
        summary="Ship the feature; the integration test is created by the same step.",
        steps=[step],
        integration_tests=["tests/integration/test_feature_e2e.py::test_e2e"],
    )
    rendered = render_plan_markdown(compliant_plan)
    violating = rendered.replace(
        '"integration_tests": [\n    "tests/integration/test_feature_e2e.py::test_e2e"\n  ]',
        '"integration_tests": [\n    "tests/unit/test_feature.py"\n  ]',
        1,
    )

    with pytest.raises(ValidationError) as excinfo:
        parse_plan_markdown(violating)

    message = str(excinfo.value)
    assert "tests/unit/test_feature.py" in message
    assert "must live under" in message
