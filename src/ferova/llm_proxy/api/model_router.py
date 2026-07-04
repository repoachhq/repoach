"""Model routing for Claude-compatible requests."""

from __future__ import annotations

import time
from dataclasses import dataclass

from loguru import logger

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.routing import ModelRef, RoutingTable, get_breaker

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


class ModelRouter:
    """Resolve incoming Claude model names to configured provider/model pairs."""

    def __init__(self, settings: Settings):
        self._table = RoutingTable.from_settings(settings)

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
