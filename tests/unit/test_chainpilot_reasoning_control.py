"""Tests for the reasoning-control policy (SP-CHAINPILOT-REASONING-CONTROL, 0b-1).

Pin the headroom math, the verified per-provider matrix, and the pure plan
policy. Purely additive — no transport is wired in this slice.
"""

from __future__ import annotations

from repoach.llm_proxy.providers.reasoning import (
    REASONING_CONTROLS,
    KnobType,
    bounded_reasoning_budget,
    plan_reasoning,
)


class TestBoundedReasoningBudget:
    def test_large_budget_is_capped(self) -> None:
        assert bounded_reasoning_budget(8000) == 2048
        assert bounded_reasoning_budget(32000) == 2048

    def test_mid_budget_takes_half(self) -> None:
        assert bounded_reasoning_budget(3000) == 1500
        assert bounded_reasoning_budget(2000) == 1000

    def test_small_budget_floors_at_256(self) -> None:
        assert bounded_reasoning_budget(300) == 256
        assert bounded_reasoning_budget(100) == 256

    def test_unusable_budget_returns_none(self) -> None:
        assert bounded_reasoning_budget(None) is None
        assert bounded_reasoning_budget(0) is None
        assert bounded_reasoning_budget(-5) is None
        assert bounded_reasoning_budget("8000") is None
        assert bounded_reasoning_budget(True) is None


class TestVerifiedMatrix:
    def test_token_budget_providers(self) -> None:
        assert REASONING_CONTROLS["nvidia_nim"].knob is KnobType.TOKEN_BUDGET
        assert REASONING_CONTROLS["open_router"].knob is KnobType.TOKEN_BUDGET
        assert REASONING_CONTROLS["nvidia_nim"].combined_budget is False

    def test_effort_providers(self) -> None:
        assert REASONING_CONTROLS["groq"].knob is KnobType.EFFORT
        assert REASONING_CONTROLS["groq"].min_effort == "low"
        assert REASONING_CONTROLS["cerebras"].min_effort == "low"
        assert REASONING_CONTROLS["deepseek"].min_effort == "high"

    def test_toggle_provider(self) -> None:
        assert REASONING_CONTROLS["kimi"].knob is KnobType.TOGGLE


class TestPlanReasoning:
    def test_token_budget_plan(self) -> None:
        plan = plan_reasoning("nvidia_nim", max_tokens=8000, thinking_enabled=True)
        assert plan.token_budget == 2048
        assert plan.effort is None
        assert plan.answer_headroom_floor is None
        assert plan.thinking_enabled is True

    def test_effort_plan_sets_min_effort_and_floor(self) -> None:
        plan = plan_reasoning("groq", max_tokens=8000, thinking_enabled=True)
        assert plan.effort == "low"
        assert plan.token_budget is None
        assert plan.answer_headroom_floor == 4096

    def test_deepseek_effort_floor_is_high(self) -> None:
        plan = plan_reasoning("deepseek", max_tokens=8000, thinking_enabled=True)
        assert plan.effort == "high"

    def test_toggle_plan_has_floor_no_budget(self) -> None:
        plan = plan_reasoning("kimi", max_tokens=8000, thinking_enabled=True)
        assert plan.token_budget is None
        assert plan.effort is None
        assert plan.answer_headroom_floor == 4096
        assert plan.thinking_enabled is True

    def test_thinking_disabled_carries_no_fields(self) -> None:
        plan = plan_reasoning("groq", max_tokens=8000, thinking_enabled=False)
        assert plan.thinking_enabled is False
        assert plan.token_budget is None
        assert plan.effort is None
        assert plan.answer_headroom_floor is None

    def test_unknown_provider_is_none_plan(self) -> None:
        plan = plan_reasoning("mystery", max_tokens=8000, thinking_enabled=True)
        assert plan.token_budget is None
        assert plan.effort is None
        assert plan.answer_headroom_floor is None
        assert plan.thinking_enabled is True
