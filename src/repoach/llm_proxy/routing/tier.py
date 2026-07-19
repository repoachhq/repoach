"""Capability tier and classification of incoming model names.

A tier names which configured chain an incoming Claude model name routes
to. :func:`classify_tier` is the substring classifier extracted verbatim
from ``Settings.resolve_models`` (``settings.py:407-420``), minus the
chain lookup: it only names the tier and is therefore pure and total.
The ``model_<tier> is None`` fallback that picks the global ``MODEL``
slot lives in the routing table (slice B), not here.
"""

from __future__ import annotations

from enum import StrEnum

from repoach.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS


class Tier(StrEnum):
    """The capability tier an incoming model name maps to.

    ``DEFAULT`` covers both the unmatched case and the H10 passthrough
    (an explicit ``provider/...`` ref), which the table resolves to the
    literal ref rather than a tier chain.
    """

    OPUS = "opus"
    SONNET = "sonnet"
    HAIKU = "haiku"
    DEFAULT = "default"


def classify_tier(model_name: str) -> Tier:
    """Classify an incoming model name to its :class:`Tier`.

    An explicit ``provider/...`` ref whose head is a supported provider
    is an H10 passthrough and classifies as :attr:`Tier.DEFAULT` (it
    bypasses the opus/sonnet/haiku substring match). Otherwise the
    name is lowercased and matched first-match-wins in the order
    ``opus`` → ``haiku`` → ``sonnet``, falling back to
    :attr:`Tier.DEFAULT`.

    Args:
        model_name: The Anthropic-style model name received by the proxy.

    Returns:
        The tier the name routes to.
    """
    if "/" in model_name:
        head = model_name.split("/", 1)[0]
        if head in SUPPORTED_PROVIDER_IDS:
            return Tier.DEFAULT
    name_lower = model_name.lower()
    if "opus" in name_lower:
        return Tier.OPUS
    if "haiku" in name_lower:
        return Tier.HAIKU
    if "sonnet" in name_lower:
        return Tier.SONNET
    return Tier.DEFAULT
