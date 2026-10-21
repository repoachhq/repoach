"""The one seam between the agent loop and a model (SP-UNIVERSAL-PROVIDERS G1).

:class:`Provider` is exactly the method :class:`~repoach.agent_engine.agent_loop.AgentLoop`
called on the gateway client before direct providers existed, so the
gateway client (:class:`~repoach.agent_engine.adapters.ProxyGatewayClient`)
satisfies it unchanged and the direct providers
(:class:`~repoach.providers.direct.DirectProvider`) slot in beside it.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from ..llm.capability import CapabilityTier
from ..llm_proxy.api.models.agent_v1 import AgentRequest, AgentResponse, Message, ToolSpec
from ..llm_proxy.api.models.anthropic import ThinkingConfig


@runtime_checkable
class Provider(Protocol):
    """A model the agent loop can call: one ``repoach/v1`` turn per :meth:`call`.

    Attributes:
        name: ``gateway``, ``claude_code`` or ``anthropic``.
        models: The model each capability tier resolves to on this provider.
    """

    name: str
    models: dict[CapabilityTier, str]

    def call(
        self,
        *,
        capability: CapabilityTier,
        system: str | None,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float,
        skip_models: frozenset[str] = frozenset(),
        thinking: ThinkingConfig | dict[str, Any] | None = None,
    ) -> AgentResponse:
        """Run one turn and return the model's answer.

        Args:
            capability: The tier; the provider maps it through :attr:`models`.
            system: Optional system prompt.
            messages: The conversation so far.
            tools: Tools the model may call.
            max_tokens: Output token cap.
            temperature: Sampling temperature.
            skip_models: Gateway-only semantic failover hint; a direct
                provider has no chain and ignores it.
            thinking: Optional thinking configuration.

        Returns:
            The parsed :class:`AgentResponse`.

        Raises:
            GatewayTransportError: Retryable transport failure.
            GatewayError: Non-retryable failure (rejected credential…).
        """
        ...


def agent_request(
    *,
    capability: CapabilityTier,
    system: str | None,
    messages: list[Message],
    tools: list[ToolSpec],
    max_tokens: int,
    temperature: float,
    skip_models: frozenset[str],
    thinking: ThinkingConfig | dict[str, Any] | None,
) -> AgentRequest:
    """Build the ``repoach/v1`` request every provider starts from.

    One construction for the gateway client (which POSTs it) and the
    direct providers (which translate it in-process), so the two routes
    can never disagree on what a turn asks for.  A ``thinking`` dict is
    coerced to :class:`ThinkingConfig`.
    """
    thinking_config: ThinkingConfig | None = None
    if thinking is not None:
        thinking_config = (
            ThinkingConfig.model_validate(thinking) if isinstance(thinking, dict) else thinking
        )
    return AgentRequest(
        schema_version="1",
        capability=capability.value,
        system=system,
        messages=list(messages),
        tools=list(tools),
        max_tokens=max_tokens,
        temperature=temperature,
        skip_models=sorted(skip_models),
        thinking=thinking_config,
    )


__all__ = ["Provider", "agent_request"]
