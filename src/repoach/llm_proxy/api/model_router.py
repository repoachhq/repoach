"""Model routing for Claude-compatible requests."""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx
from loguru import logger

from repoach.health.credits import CreditsSnapshot, get_cached_credits
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.routing import ModelRef, RoutingTable, get_breaker

from .models.anthropic import MessagesRequest, TokenCountRequest


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    original_model: str
    provider_id: str
    provider_model: str
    provider_model_ref: str


@dataclass(frozen=True, slots=True)
class RoutedMessagesRequest:
    request: MessagesRequest
    resolved: ResolvedModel


@dataclass(frozen=True, slots=True)
class RoutedTokenCountRequest:
    request: TokenCountRequest
    resolved: ResolvedModel


_last_logged_credits_snapshot: CreditsSnapshot | None = None


async def compute_credits_gate_skip_models(
    settings: Settings,
    client: httpx.AsyncClient,
    open_router_refs: frozenset[str],
) -> frozenset[str]:
    """Compute the ``open_router`` refs to exclude before the first dispatch attempt.

    SP-BREAKER-PROVIDER-SCOPE G3/G4: reads the cached OpenRouter credits
    snapshot (:func:`repoach.health.credits.get_cached_credits`, never a
    fresh poll) and, when the account balance has dropped below the
    configured floor, returns every ``open_router`` ref so the caller can
    feed them into the existing ``skip_models`` seam — keeping
    ``open_router/*`` out of dispatch before it pays a single 402
    round-trip. Fails open (returns an empty set) whenever the gate
    cannot form an opinion: no API key configured, no ``open_router``
    refs in the chain, or the snapshot is unavailable.

    Args:
        settings: Proxy settings carrying ``open_router_api_key``,
            ``credits_floor_usd``, and ``credits_health_cache_ttl_s``.
        client: The ``httpx.AsyncClient`` used to (re)fetch the snapshot
            on cache expiry.
        open_router_refs: The ``open_router`` refs present in the
            resolved chain, from :meth:`ModelRouter.open_router_refs_for`.

    Returns:
        ``open_router_refs`` unchanged when the gate should close
        (``remaining < credits_floor_usd``), otherwise an empty
        frozenset.
    """
    global _last_logged_credits_snapshot
    if not settings.open_router_api_key or not open_router_refs:
        return frozenset()
    snapshot = await get_cached_credits(
        settings.open_router_api_key,
        client=client,
        ttl_s=settings.credits_health_cache_ttl_s,
    )
    if snapshot is None:
        return frozenset()
    if snapshot.remaining < settings.credits_floor_usd:
        if _last_logged_credits_snapshot is not snapshot:
            _last_logged_credits_snapshot = snapshot
            logger.warning(
                "credits_gate_closed",
                remaining=snapshot.remaining,
                floor=settings.credits_floor_usd,
            )
        return open_router_refs
    return frozenset()


class ModelRouter:
    """Resolve incoming Claude model names to configured provider/model pairs."""

    def __init__(self, settings: Settings):
        self._table = RoutingTable.from_settings(settings)

    def open_router_refs_for(self, claude_model_name: str) -> frozenset[str]:
        """Return the ``open_router`` refs present in the unfiltered configured chain.

        Reads the chain straight from the routing table — no breaker
        state, no ``skip_models`` filtering — so the credits gate
        (SP-BREAKER-PROVIDER-SCOPE) can compute its exclusion set from
        the chain's static shape, independent of whatever is currently
        tripped or already skipped for this request.

        Args:
            claude_model_name: The client-supplied alias to resolve.

        Returns:
            The ``provider/model`` ref strings of every ``open_router``
            entry in the configured chain for ``claude_model_name``.
        """
        return frozenset(
            str(ref)
            for ref in self._table.chain_for(claude_model_name).refs
            if ref.provider_id == "open_router"
        )

    def resolve(self, claude_model_name: str) -> ResolvedModel:
        """Resolve ``claude_model_name`` to the head of its configured chain."""
        head = self._table.chain_for(claude_model_name).refs[0]
        resolved = head.to_resolved(claude_model_name)
        if resolved.provider_model != claude_model_name:
            logger.debug("MODEL MAPPING: '{}' -> '{}'", claude_model_name, resolved.provider_model)
        return resolved

    def resolve_chain(
        self,
        claude_model_name: str,
        *,
        skip_models: frozenset[str] = frozenset(),
    ) -> list[ResolvedModel]:
        """Return the ordered chain of candidates to try (SP-PROXY-CHAIN-FAILOVER).

        Every request walks the SAME configured chain — native-tool
        providers come first (they are listed first in ``MODEL_*``) and
        ``claude_code`` is the last-resort backstop, serving tool
        requests via emulation. The dispatcher retries the next
        candidate when the previous one fails at the transport layer or
        returns an empty completion.

        Args:
            claude_model_name: The client-supplied alias
                (``"opus"`` / ``"sonnet"`` / ``"haiku"`` / a real
                Anthropic model name) ``ModelRouter`` classifies via
                substring match.
            skip_models: SP-PROXY-SEMANTIC-FAILOVER — entries the caller
                has already tried and rejected on semantic grounds
                (e.g. the response was parse-unusable). Each entry is
                either the exact ``provider/model`` ref from
                ``chains.env`` or a bare model id as served back in
                ``model_used`` (``"minimaxai/minimax-m3"``): the agent
                loop only ever sees the latter, and strict ModelRef
                parsing turned its first real eviction into a 500
                (unknown provider 'minimaxai', 2026-07-04). Excluded
                from the returned chain; the chain falls back to its
                first entry if every candidate gets skipped (so the
                caller surfaces the final failure instead of looping
                on an empty chain).
        """
        chain = self._table.chain_for(claude_model_name)
        blocked: set[ModelRef] = set(get_breaker().down_refs(time.monotonic()))
        bare_model_ids: set[str] = set()
        for ref in skip_models:
            try:
                blocked.add(ModelRef.parse(ref))
            except ValueError:
                bare_model_ids.add(ref)
        if bare_model_ids:
            blocked.update(r for r in chain.refs if r.model in bare_model_ids)
        if blocked:
            chain = chain.without(frozenset(blocked))
        return [ref.to_resolved(claude_model_name) for ref in chain.refs]

    def resolve_messages_request(self, request: MessagesRequest) -> RoutedMessagesRequest:
        """Return an internal routed request context."""
        resolved = self.resolve(request.model)
        routed = request.model_copy(deep=True)
        routed.model = resolved.provider_model
        return RoutedMessagesRequest(request=routed, resolved=resolved)

    def resolve_token_count_request(self, request: TokenCountRequest) -> RoutedTokenCountRequest:
        """Return an internal token-count request context."""
        resolved = self.resolve(request.model)
        routed = request.model_copy(update={"model": resolved.provider_model}, deep=True)
        return RoutedTokenCountRequest(request=routed, resolved=resolved)
