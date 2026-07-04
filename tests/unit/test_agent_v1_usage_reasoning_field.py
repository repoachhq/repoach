"""Tests for the ``reasoning_tokens`` field on the agent_v1 ``Usage`` schema.

Part of SP-USAGE-REASONING-SPLIT step 3/4 — the schema alone must accept
an explicit ``reasoning_tokens`` value and default it to ``0`` for the
many existing callers that build ``Usage(...)`` without one, with
``total_tokens`` semantics unchanged (reasoning stays included in
``output_tokens``, per NG3).
"""

from __future__ import annotations

from ferova.llm_proxy.api.models.agent_v1 import Usage


def test_usage_reasoning_tokens_defaults_to_zero() -> None:
    """Existing callers building ``Usage`` without the field keep working."""
    usage = Usage(input_tokens=10, output_tokens=20, total_tokens=30)

    assert usage.reasoning_tokens == 0
    assert usage.total_tokens == 30


def test_usage_accepts_explicit_reasoning_tokens() -> None:
    """An explicit value round-trips and does not alter ``total_tokens``."""
    usage = Usage(
        input_tokens=10,
        output_tokens=1200,
        total_tokens=1210,
        reasoning_tokens=900,
    )

    assert usage.reasoning_tokens == 900
    assert usage.output_tokens == 1200
    assert usage.total_tokens == 1210
