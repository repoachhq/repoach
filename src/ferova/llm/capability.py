"""Capability tier — the canonical vocabulary for picking an LLM.

Every agent / session in the project declares which capability tier
it needs (``OPUS`` / ``SONNET`` / ``HAIKU``).  The local LLM proxy
walks the corresponding chain (``MODEL_OPUS`` / ``MODEL_SONNET`` /
``MODEL_HAIKU`` from ``.env``) on each request, falling over from one
upstream provider to the next on rate-limit or error.

This module holds the **only** mapping between capability tier and
the alias the proxy understands.  The mapping is hardcoded — these
aliases are the protocol vocabulary between Ferova and the proxy,
not user-facing config.  Renaming an alias would require a matching
update on the proxy's tier classifier (``routing.classify_tier``).

SP-LLM-SINGLE-SOURCE-OF-TRUTH (2026-05-05): the
``MODEL_<CAPABILITY>`` chains in ``.env`` are the ONLY model-selection
configuration in the project.  Every other surface declares a
:class:`CapabilityTier` and lets the proxy resolve the chain.

Module-level constants :
    - :data:`CAPABILITY_TO_ALIAS` maps each tier to the
      Anthropic-style alias the proxy's ``ModelRouter`` recognises.
      These names are the protocol vocabulary between ferova
      and the proxy — they are NOT user-facing config.  An operator
      who wants to swap the actual model for a tier edits the
      ``MODEL_<CAPABILITY>`` chain in ``.env``, not these aliases.
"""

from __future__ import annotations

from enum import StrEnum


class CapabilityTier(StrEnum):
    """Tiers offered by the local LLM proxy.

    Each tier maps 1:1 to one of the proxy's ``MODEL_*`` chains in
    ``.env``.  The proxy classifies inbound requests by substring
    on the model name (``"opus"`` / ``"sonnet"`` / ``"haiku"``) so the
    alias the client sends MUST contain the tier keyword for routing to
    work.

    Attributes:
        OPUS: Heavy-reasoning, frontier-quality tier — slow but
            highest accuracy.  Used by Sentinel reviewer, the
            optimizer role, and any agent that explicitly opts into
            it.  Walks ``MODEL_OPUS`` chain.
        SONNET: Balanced quality / latency — the default for
            analyst-style work (Architect / Tester / Scribe
            reviewers) and the coding agents (Planner / Coder /
            Developer).  Walks ``MODEL_SONNET`` chain.
        HAIKU: Cheap and fast — short-form summaries, the curator
            role, high-volume calls where capability isn't critical.
            Walks ``MODEL_HAIKU`` chain.
    """

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"


CAPABILITY_TO_ALIAS: dict[CapabilityTier, str] = {
    CapabilityTier.OPUS: "claude-opus-4-7",
    CapabilityTier.SONNET: "claude-sonnet-4-6",
    CapabilityTier.HAIKU: "claude-haiku-4-5",
}


def alias_for_capability(capability: CapabilityTier) -> str:
    """Return the proxy alias for a capability tier.

    Args:
        capability: Tier the caller wants.

    Returns:
        The Anthropic-style alias (e.g. ``"claude-opus-4-7"``) that
        the proxy resolves via its ``MODEL_<CAPABILITY>`` chain.
    """
    return CAPABILITY_TO_ALIAS[capability]
