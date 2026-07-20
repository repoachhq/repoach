"""Repoach agent engine — provider-agnostic agent loop.

The engine exposes :class:`agent_loop.AgentLoop`, a chain-failover
loop over the local llm_proxy sidecar (NIM, OpenRouter, claude_code).
It powers the review-bot team in :mod:`repoach.review`.
"""

from .adapters import (
    GatewayChainExhausted,
    GatewayError,
    GatewayTransportError,
    ProxyGatewayClient,
)
from .agent_loop import AgentLoop, NimAgentOutput, ToolDef

__all__ = [
    "AgentLoop",
    "GatewayChainExhausted",
    "GatewayError",
    "GatewayTransportError",
    "NimAgentOutput",
    "ProxyGatewayClient",
    "ToolDef",
]
