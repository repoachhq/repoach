"""FastAPI route handlers."""

import time

import httpx
from fastapi import APIRouter, Depends, Request, Response
from loguru import logger

from repoach.health.credits import get_cached_credits
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.core.anthropic import get_token_count
from repoach.llm_proxy.routing import RoutingTable, Tier, get_breaker

from . import dependencies
from .agent_dispatcher import dispatch_agent_request
from .dependencies import get_credits_client, get_settings, is_authenticated, require_api_key
from .models.agent_v1 import AgentRequest
from .models.anthropic import MessagesRequest, TokenCountRequest
from .models.responses import ModelResponse, ModelsListResponse
from .services import ClaudeProxyService, compute_credits_gate_skip_models

router = APIRouter()


_MODEL_ENDPOINT_TIERS: tuple[Tier, ...] = (Tier.DEFAULT, Tier.OPUS, Tier.SONNET, Tier.HAIKU)

_UNKNOWN_MODEL_CREATED_AT = "1970-01-01T00:00:00Z"


def _configured_model_ids(settings: Settings) -> list[str]:
    """Enumerate the model ids the proxy is actually configured to route to.

    Builds the same :class:`~repoach.llm_proxy.routing.RoutingTable` the
    dispatch path resolves against (SP-MODELS-ENDPOINT-TRUTHFUL) and walks
    every configured tier's full failover chain — not just its head — so
    the advertised set matches every ref the proxy could actually dispatch
    to. ``Tier.DEFAULT`` is always present (``Settings.model`` carries a
    default); ``OPUS`` / ``SONNET`` / ``HAIKU`` only contribute when their
    ``MODEL_*`` slot is configured. Ids are de-duplicated, first occurrence
    wins, preserving a stable tier-then-chain order.

    Fails closed to an empty list when the table cannot be built from the
    current settings (a malformed ``MODEL_*`` slot) rather than ever
    fabricating an id.

    Args:
        settings: The injected proxy settings.

    Returns:
        The ordered, de-duplicated ``provider/model`` ref strings.
    """
    try:
        table = RoutingTable.from_settings(settings)
    except ValueError as exc:
        logger.warning("models_endpoint_routing_table_unbuildable: {}", exc)
        return []
    seen: set[str] = set()
    model_ids: list[str] = []
    for tier in _MODEL_ENDPOINT_TIERS:
        chain = table.chains.get(tier)
        if chain is None:
            continue
        for ref in chain.refs:
            ref_id = str(ref)
            if ref_id in seen:
                continue
            seen.add(ref_id)
            model_ids.append(ref_id)
    return model_ids


def get_proxy_service(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> ClaudeProxyService:
    """Build the request service for route handlers."""
    return ClaudeProxyService(
        settings,
        provider_getter=lambda provider_type: dependencies.resolve_provider(
            provider_type, app=request.app, settings=settings
        ),
        token_counter=get_token_count,
    )


def _probe_response(allow: str) -> Response:
    """Return an empty success response for compatibility probes."""
    return Response(status_code=204, headers={"Allow": allow})


@router.post("/v1/messages")
async def create_message(
    request_data: MessagesRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_credits_client),
    _auth=Depends(require_api_key),
):
    """Create a message (always streaming).

    Before the first dispatch attempt, SP-BREAKER-PROVIDER-SCOPE's
    proactive credits gate consults the cached OpenRouter balance
    snapshot and, when it is below the configured floor, excludes
    every ``open_router`` ref configured for this model from the
    chain via the existing ``skip_models`` seam — keeping a dead
    account out of dispatch before it pays a single 402 round-trip.
    """
    open_router_refs = service.open_router_refs_for(request_data.model)
    gated_skip = await compute_credits_gate_skip_models(settings, client, open_router_refs)
    return service.create_message(request_data, skip_models=gated_skip)


@router.api_route("/v1/messages", methods=["HEAD", "OPTIONS"])
async def probe_messages(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the messages endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/agent")
async def create_agent_turn(
    request_data: AgentRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """Capability-gateway endpoint (SP-CAPABILITY-GATEWAY).

    Accepts a ``repoach/v1`` request, walks the chain stored in
    MODEL_<capability.upper()>, and returns a single-turn
    ``AgentResponse``.  The agentic loop (multi-turn tool_use ↔
    tool_result) lives in the client SDK, not here.
    """
    return await dispatch_agent_request(request_data, service)


@router.api_route("/v1/agent", methods=["HEAD", "OPTIONS"])
async def probe_agent(_auth=Depends(require_api_key)):
    """Respond to compatibility probes for the agent endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.post("/v1/messages/count_tokens")
async def count_tokens(
    request_data: TokenCountRequest,
    service: ClaudeProxyService = Depends(get_proxy_service),
    _auth=Depends(require_api_key),
):
    """Count tokens for a request."""
    return service.count_tokens(request_data)


@router.api_route("/v1/messages/count_tokens", methods=["HEAD", "OPTIONS"])
async def probe_count_tokens(_auth=Depends(require_api_key)):
    """Respond to Claude compatibility probes for the token count endpoint."""
    return _probe_response("POST, HEAD, OPTIONS")


@router.get("/")
async def root(settings: Settings = Depends(get_settings), _auth=Depends(require_api_key)):
    """Root endpoint."""
    return {
        "status": "ok",
        "provider": settings.provider_type,
        "model": settings.model,
    }


@router.api_route("/", methods=["HEAD", "OPTIONS"])
async def probe_root(_auth=Depends(require_api_key)):
    """Respond to compatibility probes for the root endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/health")
async def health(
    settings: Settings = Depends(get_settings),
    client: httpx.AsyncClient = Depends(get_credits_client),
    authenticated: bool = Depends(is_authenticated),
):
    """Health check endpoint.

    Anonymous callers get liveness only — ``{"status": "healthy"}`` —
    never the breaker/credits internals (SP-PROXY-EDGE-HARDEN F-HEALTH:
    those internals disclose live routing topology and failure state, an
    unauthenticated-disclosure surface on a public bind). A caller that
    presents a valid API key (or any caller when no
    ``anthropic_auth_token`` is configured, matching
    :func:`~repoach.llm_proxy.api.dependencies.require_api_key`'s
    no-op-when-unconfigured rule) additionally gets:

    * ``breaker`` — the failover breaker's current state
      (SP-CHAIN-DEAD-HOP-QUARANTINE), one entry per currently-down ref
      with ``ref``, ``reason``, ``ttl_remaining_s``, and
      ``consecutive_failures``; empty when nothing is tripped.
    * ``credits`` — the OpenRouter account balance (SP-CREDITS-CHECK)
      when ``open_router_api_key`` is configured and a snapshot is
      available; ``null`` when the key is absent or the fetch fails.
    """
    if not authenticated:
        return {"status": "healthy"}

    breaker_entries = get_breaker().snapshot(time.monotonic())
    credits = None
    if settings.open_router_api_key:
        snapshot = await get_cached_credits(
            settings.open_router_api_key,
            client=client,
            ttl_s=settings.credits_health_cache_ttl_s,
            timeout_s=3.0,
        )
        if snapshot is not None:
            credits = {
                "open_router": {
                    "total_credits": snapshot.total_credits,
                    "total_usage": snapshot.total_usage,
                    "remaining": snapshot.remaining,
                }
            }
    return {
        "status": "healthy",
        "breaker": [
            {
                "ref": str(entry.ref),
                "reason": entry.reason,
                "ttl_remaining_s": entry.ttl_remaining_s,
                "consecutive_failures": entry.consecutive_failures,
            }
            for entry in breaker_entries
        ],
        "credits": credits,
    }


@router.api_route("/health", methods=["HEAD", "OPTIONS"])
async def probe_health():
    """Respond to compatibility probes for the health endpoint."""
    return _probe_response("GET, HEAD, OPTIONS")


@router.get("/v1/models", response_model=ModelsListResponse)
async def list_models(
    settings: Settings = Depends(get_settings),
    _auth=Depends(require_api_key),
):
    """List the model ids this proxy is actually configured to route to.

    Derived from the configured ``MODEL_*`` routing chains
    (SP-MODELS-ENDPOINT-TRUTHFUL) via :func:`_configured_model_ids`
    instead of a static literal, so a client never sees an id this
    proxy cannot actually dispatch to.
    """
    model_ids = _configured_model_ids(settings)
    data = [
        ModelResponse(
            id=model_id,
            display_name=model_id,
            created_at=_UNKNOWN_MODEL_CREATED_AT,
        )
        for model_id in model_ids
    ]
    return ModelsListResponse(
        data=data,
        first_id=data[0].id if data else None,
        has_more=False,
        last_id=data[-1].id if data else None,
    )
