"""Application services for the Claude-compatible API."""

from __future__ import annotations

import time
import traceback
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from loguru import logger

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.core.anthropic import get_token_count, get_user_facing_error_message
from repoach.llm_proxy.providers.base import BaseProvider
from repoach.llm_proxy.providers.exceptions import (
    APIError,
    AuthenticationError,
    InvalidRequestError,
    OverloadedError,
    ProviderError,
    RateLimitError,
)
from repoach.llm_proxy.routing import ModelRef, get_breaker, ttl_for_reason
from repoach.llm_proxy.routing.breaker import ACCOUNT_FAULT_REASONS, escalated_ttl

from ._failover import PeekResult, peek_for_content
from .model_router import ModelRouter, ResolvedModel, compute_credits_gate_skip_models
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.responses import TokenCountResponse

__all__ = ["ClaudeProxyService", "compute_credits_gate_skip_models"]

TokenCounter = Callable[[list[Any], str | list[Any] | None, list[Any] | None], int]

ProviderGetter = Callable[[str], BaseProvider]


def _classify_failover_reason(exc: BaseException) -> str:
    """Classify a failover-triggering exception into a spec-vocabulary reason.

    Returns one of: ``timeout``, ``rate_limited``, ``provider_5xx``,
    ``provider_4xx``, ``auth_failed``, ``invalid_request``,
    ``transport_error``, or ``exception:<TypeName>`` as the fallback.

    The vocabulary mirrors SP-LLM-PROXY-FAILOVER-LOG so operator
    dashboards can group fallbacks by upstream symptom regardless of
    which provider raised them.
    """
    exc_name = type(exc).__name__
    name_lower = exc_name.lower()
    if "timeout" in name_lower:
        return "timeout"
    if isinstance(exc, RateLimitError):
        return "rate_limited"
    if isinstance(exc, OverloadedError):
        return "provider_5xx"
    if isinstance(exc, AuthenticationError):
        return "auth_failed"
    if isinstance(exc, InvalidRequestError):
        return "invalid_request"
    if isinstance(exc, APIError):
        status = getattr(exc, "status_code", None)
        if isinstance(status, int):
            if 500 <= status < 600:
                return "provider_5xx"
            if 400 <= status < 500:
                return f"provider_{status}"
        return "api_error"
    if isinstance(exc, ProviderError):
        return "provider_error"
    if any(
        keyword in name_lower
        for keyword in ("transport", "connection", "disconnect", "protocol", "network")
    ):
        return "transport_error"
    return f"exception:{exc_name}"


class ClaudeProxyService:
    """Coordinate request optimization, model routing, token count, and providers."""

    def __init__(
        self,
        settings: Settings,
        provider_getter: ProviderGetter,
        model_router: ModelRouter | None = None,
        token_counter: TokenCounter = get_token_count,
    ):
        self._settings = settings
        self._provider_getter = provider_getter
        self._model_router = model_router or ModelRouter(settings)
        self._token_counter = token_counter

    def open_router_refs_for(self, claude_model_name: str) -> frozenset[str]:
        """Return the ``open_router`` refs configured for ``claude_model_name``.

        Thin passthrough to :meth:`ModelRouter.open_router_refs_for` that
        keeps ``_model_router`` private while giving route handlers
        (SP-BREAKER-PROVIDER-SCOPE) a seam to compute the proactive
        credits-gate exclusion set ahead of dispatch.

        Args:
            claude_model_name: The client-supplied alias to resolve.

        Returns:
            The ``provider/model`` ref strings of every ``open_router``
            entry in the configured chain for ``claude_model_name``.
        """
        return self._model_router.open_router_refs_for(claude_model_name)

    def create_message(
        self,
        request_data: MessagesRequest,
        *,
        skip_models: frozenset[str] = frozenset(),
    ) -> StreamingResponse:
        """Create a chain-walked streaming response.

        Every streaming response is routed
        through :meth:`_stream_with_failover` so the chain-failover
        layer catches transport flaps uniformly for tools-using
        callers (the agent loop) and tools-less ones (reviewers).
        Both walk the SAME chain from
        :meth:`ModelRouter.resolve_chain`; ``claude_code`` stays in it
        as the last-resort backstop and serves tool requests via
        emulation — no per-request filtering.

        Args:
            request_data: Anthropic-shaped ``MessagesRequest`` carrying
                the model alias, messages, optional tools, and
                streaming flag.
            skip_models: SP-PROXY-SEMANTIC-FAILOVER — provider-prefixed
                model refs (the exact ``provider/model`` strings from
                ``chains.env``) the caller has already semantically
                rejected on this retry chain.  Forwarded to
                :meth:`ModelRouter.resolve_chain` so those candidates
                are filtered out of the walk ; defaults to an empty
                frozenset which preserves the full configured chain.

        Returns:
            A :class:`fastapi.responses.StreamingResponse` carrying the
            chain-walked Anthropic SSE stream.

        Raises:
            HTTPException: ``400`` for invalid requests (e.g. empty
                ``messages``), ``502`` when every chain candidate
                returns an empty completion, or the status code
                carried by the underlying provider exception when
                one is raised.
            ProviderError: Surfaced unchanged so the caller can
                distinguish provider-level failures from generic
                server errors.
        """
        try:
            if not request_data.messages:
                raise InvalidRequestError("messages cannot be empty")

            routed = self._model_router.resolve_messages_request(request_data)
            input_tokens = self._token_counter(
                routed.request.messages, routed.request.system, routed.request.tools
            )
            chain = self._model_router.resolve_chain(
                request_data.model,
                skip_models=skip_models,
            )
            logger.info(
                "CHAIN_RESOLVED: alias={} skip_models={} chain_len={} chain=[{}]",
                request_data.model,
                sorted(skip_models) if skip_models else "[]",
                len(chain),
                ", ".join(c.provider_model_ref for c in chain),
            )
            return StreamingResponse(
                self._stream_with_failover(request_data, chain, input_tokens=input_tokens),
                media_type="text/event-stream",
                headers={
                    "X-Accel-Buffering": "no",
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                },
            )

        except ProviderError:
            raise
        except Exception as e:
            logger.error(f"Error: {e!s}\n{traceback.format_exc()}")
            raise HTTPException(
                status_code=getattr(e, "status_code", 500),
                detail=get_user_facing_error_message(e),
            ) from e

    def _trip_breaker(
        self,
        candidate: ResolvedModel,
        reason: str,
        *,
        chain: list[ResolvedModel] | None = None,
    ) -> None:
        """Mark a failed candidate down so the next request skips it.

        Gated by ``breaker_enabled``.  Composes the quarantine escalation
        policy (SP-CHAIN-DEAD-HOP-QUARANTINE):

        1. Compute a reason-aware base TTL via
           :func:`repoach.llm_proxy.routing.breaker.ttl_for_reason`
           (terminal beats quarantine beats default).
        2. Peek at the ref's current consecutive-failure count, then
           apply :func:`repoach.llm_proxy.routing.breaker.escalated_ttl`
           so the Nth consecutive failure escalates to the quarantine
           TTL regardless of the original reason.
        3. When ``reason`` is an account-class fault and ``chain`` is
           supplied, propagate to every sibling ref of the same provider
           via :meth:`BreakerState.trip_provider` and return early
           with a single ``breaker_provider_propagated`` log event
           (SP-BREAKER-PROVIDER-SCOPE G1).
        4. Otherwise, trip once at the effective TTL (no
           double-increment) and emit a structured
           ``breaker_quarantined`` log event exactly when the applied
           TTL is the quarantine one.

        Closes the health loop — see :mod:`repoach.llm_proxy.routing.breaker`.
        """
        if not self._settings.breaker_enabled:
            return
        ref = ModelRef.parse(candidate.provider_model_ref)
        breaker = get_breaker()
        reason_ttl = ttl_for_reason(
            reason,
            default_ttl_s=self._settings.breaker_ttl_s,
            terminal_ttl_s=self._settings.breaker_ttl_terminal_s,
            quarantine_ttl_s=self._settings.breaker_ttl_quarantine_s,
        )
        current_count = breaker._consecutive_failures.get(ref, 0)
        would_be_count = current_count + 1
        effective_ttl = escalated_ttl(
            would_be_count,
            base_ttl_s=reason_ttl,
            quarantine_ttl_s=self._settings.breaker_ttl_quarantine_s,
            threshold=self._settings.breaker_quarantine_threshold,
        )
        if reason in ACCOUNT_FAULT_REASONS and chain is not None:
            sibling_refs = {
                ModelRef.parse(c.provider_model_ref)
                for c in chain
                if c.provider_id == ref.provider_id
            }
            breaker.trip_provider(
                ref.provider_id,
                sibling_refs,
                now=time.monotonic(),
                ttl_s=effective_ttl,
                reason=f"{reason}_propagated",
            )
            logger.warning(
                "breaker_provider_propagated",
                provider=ref.provider_id,
                ref_count=len(sibling_refs),
                ttl_s=effective_ttl,
            )
            return
        breaker.trip(ref, now=time.monotonic(), ttl_s=effective_ttl, reason=reason)
        if effective_ttl == self._settings.breaker_ttl_quarantine_s:
            logger.warning(
                "breaker_quarantined",
                ref=str(ref),
                reason=reason,
                count=would_be_count,
                ttl_s=effective_ttl,
            )

    async def _stream_with_failover(
        self,
        original_request: MessagesRequest,
        chain: list[ResolvedModel],
        *,
        input_tokens: int,
    ) -> AsyncIterator[str]:
        """Iterate ``chain`` until a candidate provider yields real content.

        For each candidate the dispatcher peeks at the head of the SSE
        stream; on transient failure (transport error or empty
        completion before any content) it emits a structured
        ``proxy_chain_failover_fired`` event and tries the next
        entry. When the whole chain is exhausted without a usable
        response, a single ``proxy_chain_exhausted`` event is emitted
        before the HTTP error propagates — see SP-LLM-PROXY-FAILOVER-LOG
        for the rationale.
        """
        dispatch_id = f"disp_{uuid.uuid4().hex[:12]}"
        last_error: Exception | None = None
        prior_failures: list[tuple[str, str]] = []
        for attempt_index, candidate in enumerate(chain):
            candidate_ref = ModelRef.parse(candidate.provider_model_ref)
            if get_breaker().is_down(candidate_ref, time.monotonic()):
                logger.info(
                    "proxy_chain_skip_tripped",
                    dispatch_id=dispatch_id,
                    candidate=candidate.provider_model_ref,
                    attempt=attempt_index + 1,
                )
                continue
            request_id = f"req_{uuid.uuid4().hex[:12]}"
            attempt_request = original_request.model_copy(
                update={"model": candidate.provider_model}, deep=True
            )
            logger.info(
                "API_REQUEST: request_id={} model={} messages={} attempt={}/{}",
                request_id,
                attempt_request.model,
                len(attempt_request.messages),
                attempt_index + 1,
                len(chain),
            )
            logger.debug("FULL_PAYLOAD [{}]: {}", request_id, attempt_request.model_dump())
            attempt_started = time.monotonic()
            try:
                provider = self._provider_getter(candidate.provider_id)
                stream = provider.stream_response(
                    attempt_request,
                    input_tokens=input_tokens,
                    request_id=request_id,
                )
                peek = await peek_for_content(stream)
            except Exception as exc:
                attempt_latency_s = round(time.monotonic() - attempt_started, 3)
                last_error = exc
                primary_reason = _classify_failover_reason(exc)
                prior_failures.append((candidate.provider_model_ref, primary_reason))
                logger.warning(
                    "proxy_chain_failover_fired",
                    dispatch_id=dispatch_id,
                    request_id=request_id,
                    attempt=attempt_index + 1,
                    chain_length=len(chain),
                    chain_remaining=len(chain) - attempt_index - 1,
                    primary=candidate.provider_model_ref,
                    primary_reason=primary_reason,
                    primary_error_type=type(exc).__name__,
                    primary_error=str(exc)[:200],
                    latency_s=attempt_latency_s,
                )
                self._trip_breaker(candidate, primary_reason, chain=chain)
                continue

            attempt_latency_s = round(time.monotonic() - attempt_started, 3)

            if peek.got_content:
                get_breaker().recover(ModelRef.parse(candidate.provider_model_ref))
                if attempt_index > 0:
                    logger.info(
                        "proxy_chain_failover_recovered",
                        dispatch_id=dispatch_id,
                        request_id=request_id,
                        served_by=candidate.provider_model_ref,
                        attempt=attempt_index + 1,
                        earlier_failures=attempt_index,
                        prior_failures=prior_failures,
                        latency_s=attempt_latency_s,
                    )
                for buffered_chunk in peek.buffered:
                    yield buffered_chunk
                async for chunk in stream:
                    yield chunk
                return

            if self._settings.budget_retry_enabled and peek.looks_budget_starved:
                retry_peek = await self._retry_with_more_budget(
                    candidate=candidate,
                    attempt_request=attempt_request,
                    input_tokens=input_tokens,
                    dispatch_id=dispatch_id,
                    attempt_index=attempt_index,
                )
                if retry_peek is not None and retry_peek.got_content:
                    for buffered_chunk in retry_peek.buffered:
                        yield buffered_chunk
                    return

            prior_failures.append((candidate.provider_model_ref, "empty_completion"))
            logger.warning(
                "proxy_chain_failover_fired",
                dispatch_id=dispatch_id,
                request_id=request_id,
                attempt=attempt_index + 1,
                chain_length=len(chain),
                chain_remaining=len(chain) - attempt_index - 1,
                primary=candidate.provider_model_ref,
                primary_reason="empty_completion",
                stream_done=peek.stream_done,
                buffered_events=len(peek.buffered),
                latency_s=attempt_latency_s,
            )
            self._trip_breaker(candidate, "empty_completion", chain=chain)

        logger.error(
            "proxy_chain_exhausted",
            dispatch_id=dispatch_id,
            chain_length=len(chain),
            attempts=len(chain),
            attempted=[c.provider_model_ref for c in chain],
            failures=prior_failures,
            last_error_type=type(last_error).__name__ if last_error else None,
            last_error_message=str(last_error)[:200] if last_error else None,
        )
        if last_error is not None:
            raise last_error
        raise HTTPException(
            status_code=502,
            detail=(
                "All "
                f"{len(chain)} provider candidate(s) returned empty completions. "
                "See proxy logs (proxy_chain_exhausted) for the per-attempt outcome."
            ),
        )

    async def _retry_with_more_budget(
        self,
        *,
        candidate: ResolvedModel,
        attempt_request: MessagesRequest,
        input_tokens: int,
        dispatch_id: str,
        attempt_index: int,
    ) -> PeekResult | None:
        """Re-issue a budget-starved candidate once with a larger ``max_tokens``.

        A thinking model whose hidden reasoning consumed the whole budget
        returns an empty completion that :func:`peek_for_content` flags as
        ``looks_budget_starved``.  Rather than fail over — discarding a
        capable model that only needed headroom — retry the SAME candidate
        with ``max_tokens`` enlarged by ``budget_retry_factor`` (floored
        and capped).  Returns the retry's :class:`PeekResult`, or ``None``
        when no enlargement is possible or the retry raised.
        """
        original_max = attempt_request.max_tokens
        if original_max is None:
            enlarged = self._settings.budget_retry_cap
        else:
            enlarged = min(
                max(
                    original_max * self._settings.budget_retry_factor,
                    self._settings.budget_retry_floor,
                ),
                self._settings.budget_retry_cap,
            )
            if enlarged <= original_max:
                return None
        retry_request = attempt_request.model_copy(update={"max_tokens": enlarged}, deep=True)
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        retry_started = time.monotonic()
        try:
            provider = self._provider_getter(candidate.provider_id)
            stream = provider.stream_response(
                retry_request, input_tokens=input_tokens, request_id=request_id
            )
            retry_peek = await peek_for_content(stream)
        except Exception as exc:
            logger.warning(
                "proxy_budget_retry_failed",
                dispatch_id=dispatch_id,
                request_id=request_id,
                candidate=candidate.provider_model_ref,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
                latency_s=round(time.monotonic() - retry_started, 3),
            )
            return None
        logger.info(
            "proxy_budget_retry",
            dispatch_id=dispatch_id,
            request_id=request_id,
            candidate=candidate.provider_model_ref,
            attempt=attempt_index + 1,
            original_max_tokens=original_max,
            enlarged_max_tokens=enlarged,
            got_content=retry_peek.got_content,
            latency_s=round(time.monotonic() - retry_started, 3),
        )
        return retry_peek

    def count_tokens(self, request_data: TokenCountRequest) -> TokenCountResponse:
        """Count tokens for a request after applying configured model routing."""
        request_id = f"req_{uuid.uuid4().hex[:12]}"
        with logger.contextualize(request_id=request_id):
            try:
                routed = self._model_router.resolve_token_count_request(request_data)
                tokens = self._token_counter(
                    routed.request.messages, routed.request.system, routed.request.tools
                )
                logger.info(
                    "COUNT_TOKENS: request_id={} model={} messages={} input_tokens={}",
                    request_id,
                    routed.request.model,
                    len(routed.request.messages),
                    tokens,
                )
                return TokenCountResponse(input_tokens=tokens)
            except Exception as e:
                logger.error(
                    "COUNT_TOKENS_ERROR: request_id={} error={}\n{}",
                    request_id,
                    get_user_facing_error_message(e),
                    traceback.format_exc(),
                )
                raise HTTPException(status_code=500, detail=get_user_facing_error_message(e)) from e
