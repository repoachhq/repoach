"""A generic OpenAI-compatible provider.

Serves any upstream that speaks the OpenAI chat-completions API with no
bespoke request shaping — kimi (Moonshot), groq, cerebras, and deepseek. It
reuses :class:`OpenAIChatTransport` and builds the request body via the shared
Anthropic→OpenAI conversion, so registering one of these providers is a
catalog descriptor plus a one-line factory, with no new transport module per
provider.

It applies the reasoning plan (SP-CHAINPILOT-REASONING-CONTROL): the
``max_tokens`` headroom floor (these providers share their completion budget
between reasoning and answer), the thinking-disable toggle on the providers that
accept it, and the per-model ``reasoning_effort`` knob — applied only for the
``(provider, model)`` cells the probe matrix observed to reason safely at that
effort (SP-CHAINPILOT-EFFORT-APPLY, 2a-3-iv-b, closing the 0b-3 deferral). See
:meth:`GenericOpenAIProvider._apply_reasoning_plan`.

Providers that need bespoke request shaping (NIM's ``chat_template_kwargs`` and
reasoning budget) keep their own subclass instead.
"""

from __future__ import annotations

from typing import Any

from ferova.llm_proxy.core.anthropic import build_base_request_body
from ferova.llm_proxy.providers.base import ProviderConfig
from ferova.llm_proxy.providers.effort_knob import effort_extra_body
from ferova.llm_proxy.providers.effort_map import get_effort_map
from ferova.llm_proxy.providers.openai_compat import OpenAIChatTransport
from ferova.llm_proxy.providers.reasoning import ReasoningPlan, plan_reasoning

_TOGGLE_DISABLE_PROVIDERS = frozenset({"kimi", "deepseek"})
"""Providers doc-verified (2026-06-22) to accept ``thinking:{type:"disabled"}``.

groq / cerebras disable reasoning via a per-model ``reasoning_effort`` value
instead, which is deferred with the rest of the effort knob.
"""


class GenericOpenAIProvider(OpenAIChatTransport):
    """OpenAI-compatible provider applying only the safe reasoning-plan parts."""

    def __init__(self, config: ProviderConfig, *, provider_name: str):
        super().__init__(
            config,
            provider_name=provider_name,
            base_url=config.base_url or "",
            api_key=config.api_key,
        )

    def _build_request_body(self, request: Any) -> dict:
        """Convert the Anthropic request to an OpenAI chat body and shape reasoning."""
        thinking_enabled = self._is_thinking_enabled(request)
        body = build_base_request_body(request, include_thinking=thinking_enabled)
        plan = plan_reasoning(
            self._provider_name,
            max_tokens=body.get("max_tokens"),
            thinking_enabled=thinking_enabled,
        )
        self._apply_reasoning_plan(body, plan)
        return body

    def _apply_reasoning_plan(self, body: dict, plan: ReasoningPlan) -> None:
        """Apply *plan* to *body*.

        Three levers:

        * ``answer_headroom_floor`` — raise ``max_tokens`` to the floor so a
          combined-budget provider leaves room for the answer beyond reasoning.
        * ``reasoning_effort`` — apply the per-model effort, but only when the
          plan calls for effort (``plan.effort is not None``: an EFFORT provider
          with thinking on) AND the runtime effort map (seeded from the probe
          matrix) resolved a non-``None`` effort for this exact
          ``(provider, model)`` cell. An unseeded / unresolved cell applies
          nothing, so behavior matches 0b-3 until a sweep has observed the cell.
        * thinking disable — on :data:`_TOGGLE_DISABLE_PROVIDERS` only.
        """
        if plan.answer_headroom_floor is not None:
            current = body.get("max_tokens")
            if not isinstance(current, int) or current < plan.answer_headroom_floor:
                body["max_tokens"] = plan.answer_headroom_floor

        if plan.effort is not None:
            resolved = get_effort_map().effort_for(self._provider_name, str(body.get("model", "")))
            if resolved is not None:
                extra_body = body.setdefault("extra_body", {})
                if isinstance(extra_body, dict):
                    extra_body.update(effort_extra_body(self._provider_name, resolved))

        if not plan.thinking_enabled and self._provider_name in _TOGGLE_DISABLE_PROVIDERS:
            extra_body = body.setdefault("extra_body", {})
            if isinstance(extra_body, dict):
                extra_body["thinking"] = {"type": "disabled"}
