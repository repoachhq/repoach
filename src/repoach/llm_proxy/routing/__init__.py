"""Routing domain layer.

Value objects for the LLM proxy's request routing: a validated
``provider/model`` reference (:class:`ModelRef`), an ordered failover
chain (:class:`Chain`), the tier-to-chain table (:class:`RoutingTable`),
and the capability tier (:class:`Tier`) with its classifier
(:func:`classify_tier`).

This package is the clean-slate home of the routing domain introduced by
the ``SP-ROUTING-*`` arc (umbrella:
``docs/proxy_routing_redesign_architecture.md``). It deliberately holds
no import from :mod:`repoach.llm_proxy.api` so the routing types stay
free of the dispatch layer; the boundary adapter
:meth:`ModelRef.to_resolved` imports the legacy ``ResolvedModel`` lazily.
"""

from __future__ import annotations

from repoach.llm_proxy.routing.breaker import (
    TERMINAL_REASONS,
    BreakerState,
    get_breaker,
    reset_breaker,
    ttl_for_reason,
)
from repoach.llm_proxy.routing.chain import Chain
from repoach.llm_proxy.routing.refs import ModelRef
from repoach.llm_proxy.routing.slow_policy import is_slow_completion
from repoach.llm_proxy.routing.table import RoutingTable
from repoach.llm_proxy.routing.tier import Tier, classify_tier

__all__ = [
    "TERMINAL_REASONS",
    "BreakerState",
    "Chain",
    "ModelRef",
    "RoutingTable",
    "Tier",
    "classify_tier",
    "get_breaker",
    "is_slow_completion",
    "reset_breaker",
    "ttl_for_reason",
]
