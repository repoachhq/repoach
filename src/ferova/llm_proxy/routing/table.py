"""The routing table: a tier-to-chain map built once from settings.

:class:`RoutingTable` is the single source of truth for "which chain
does this incoming model name route to", replacing the string logic
scattered across ``Settings.resolve_models`` / ``_split_chain`` and the
duplicate resolution in ``ModelRouter``. It is built once
(:meth:`from_settings`), so a malformed ref fails loudly at build time
rather than mid-failover.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ferova.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.routing.chain import Chain
from ferova.llm_proxy.routing.refs import ModelRef
from ferova.llm_proxy.routing.tier import Tier, classify_tier


@dataclass(frozen=True, slots=True)
class RoutingTable:
    """An immutable ``Tier -> Chain`` map; always holds ``Tier.DEFAULT``."""

    chains: Mapping[Tier, Chain]

    @classmethod
    def from_settings(cls, settings: Settings) -> RoutingTable:
        """Build the table from the configured ``MODEL_*`` slots.

        ``Tier.DEFAULT`` maps to the global ``MODEL`` chain and is always
        present; each tier slot (``model_opus`` / ``model_sonnet`` /
        ``model_haiku``) that is configured (non-``None``,
        non-blank) adds its own chain. An unconfigured slot is simply
        absent, so :meth:`chain_for` falls back to ``DEFAULT`` for it —
        matching the legacy ``model_<tier> is not None`` guard.

        Args:
            settings: The proxy settings carrying the slot values.

        Returns:
            The built routing table.
        """
        chains: dict[Tier, Chain] = {Tier.DEFAULT: Chain.parse(settings.model)}
        slots: dict[Tier, str | None] = {
            Tier.OPUS: settings.model_opus,
            Tier.SONNET: settings.model_sonnet,
            Tier.HAIKU: settings.model_haiku,
        }
        for tier, spec in slots.items():
            if spec and spec.strip():
                chains[tier] = Chain.parse(spec)
        return cls(chains=MappingProxyType(chains))

    def chain_for(self, model_name: str) -> Chain:
        """Resolve an incoming model name to its failover chain.

        Reproduces ``Settings.resolve_models``: an explicit
        ``provider/...`` ref (H10 passthrough) returns the literal ref as
        a single-entry chain; otherwise the name is classified to a
        :class:`Tier` and routed to that tier's chain, falling back to the
        ``DEFAULT`` chain when the tier is unconfigured.

        Args:
            model_name: The Anthropic-style model name received by the proxy.

        Returns:
            The chain to walk for this name.
        """
        if "/" in model_name:
            head = model_name.split("/", 1)[0]
            if head in SUPPORTED_PROVIDER_IDS:
                return Chain(refs=(ModelRef.parse(model_name),))
        tier = classify_tier(model_name)
        return self.chains.get(tier, self.chains[Tier.DEFAULT])
