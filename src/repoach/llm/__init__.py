"""LLM layer — capability tiers for the local proxy.

Every LLM call in Ferova goes through the local ``free-claude-code``
proxy (``http://localhost:8082``), which transparently routes
Anthropic-format requests to a free backend (NVIDIA NIM, OpenRouter,
claude_code). The user's paid Anthropic quota is not consumed.

This package holds the capability-tier vocabulary
(:class:`capability.CapabilityTier`) shared between the
:class:`repoach.agent_engine.AgentLoop` and the proxy's router.
"""

from .capability import CAPABILITY_TO_ALIAS, CapabilityTier, alias_for_capability

__all__ = [
    "CAPABILITY_TO_ALIAS",
    "CapabilityTier",
    "alias_for_capability",
]
