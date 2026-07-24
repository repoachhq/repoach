"""Thin HTTP client for the proxy's ``repoach/v1`` capability gateway.

SP-LLM-NATIVE-FORMAT (2026-05-06): the agent process speaks
``repoach/v1`` natively — :class:`AgentRequest` /
:class:`AgentResponse` Pydantic models flow end-to-end with no
OpenAI-shape translation in the middle.  This module is just the
HTTP transport for that protocol.

The previous file carried two adapter classes that re-implemented
the OpenAI Chat Completions surface in-process
(``OpenAIAdapter``, the OpenAI-shaped translation in
``ProxyGatewayAdapter``).  Both are deleted — the agent loop now
constructs ``AgentRequest`` directly and reads
``AgentResponse.content`` blocks (``TextBlock`` / ``ToolCallBlock``)
without any ``SimpleNamespace`` glue.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from ..llm_proxy.api.models.agent_v1 import (
    AgentRequest,
    AgentResponse,
    Message,
    ToolSpec,
)
from ..llm_proxy.api.models.anthropic import ThinkingConfig

_log = get_logger(__name__)


class GatewayError(Exception):
    """Base class for proxy-gateway errors raised to the agent loop."""


class GatewayTransportError(GatewayError):
    """5xx, 429, connect-refused, read-timeout — retryable transport-level."""


class GatewayChainExhausted(GatewayError):
    """The proxy walked its chain and every upstream returned an error.

    Distinct from :class:`GatewayTransportError` because the failover
    is the proxy's job — when this fires, the proxy already gave up
    on every entry in ``MODEL_<CAPABILITY>``.  Retrying from the
    agent side won't help.
    """


class ProxyGatewayClient:
    """Native ``repoach/v1`` HTTP client.

    :meth:`call` POSTs an :class:`AgentRequest` to
    ``{base_url}/v1/agent`` and parses the response as an
    :class:`AgentResponse`.  Errors raise typed exceptions so the
    agent loop can decide when to retry vs surface to the caller.
    """

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        timeout_s: float,
    ) -> None:
        """Initialise the client.

        Args:
            base_url: HTTP root of the proxy
                (``"http://localhost:8082"``).  ``/v1/agent`` is
                appended at call time.
            api_key: Bearer token sent as both ``Authorization:
                Bearer …`` and ``X-Api-Key`` (the proxy accepts
                either).
            timeout_s: Per-request HTTP timeout in seconds —
                applied as both connect and read budget.
        """
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._timeout_s = timeout_s

    def call(
        self,
        *,
        capability: CapabilityTier,
        system: str | None,
        messages: list[Message],
        tools: list[ToolSpec],
        max_tokens: int,
        temperature: float,
        skip_models: frozenset[str] = frozenset(),
        thinking: ThinkingConfig | dict[str, Any] | None = None,
    ) -> AgentResponse:
        """POST one request to ``/v1/agent`` and return the parsed response.

        Args:
            capability: Tier the proxy should resolve.
            system: Optional top-level system message.  Embedding
                ``role:"system"`` inside ``messages`` is rejected by
                the gateway schema; this lifts it to where it belongs.
            messages: Ordered conversation history as :class:`Message`
                objects.
            tools: Tool specs the model may invoke.  Empty list is
                fine.
            max_tokens: Output token cap.
            temperature: Sampling temperature.
            skip_models: SP-PROXY-SEMANTIC-FAILOVER — provider-prefixed
                model refs (the exact ``provider/model`` strings from
                ``chains.env``) the caller has already semantically
                rejected on this retry chain.  Forwarded to the proxy
                via :attr:`AgentRequest.skip_models` so
                :meth:`ModelRouter.resolve_chain` filters them out.
                Defaults to an empty frozenset for tools-less and
                non-reviewer callers.
            thinking: Optional thinking config forwarded verbatim to
                the gateway.  A dict is coerced to
                :class:`ThinkingConfig`; ``None`` (default) means
                today's behaviour — no thinking field on the
                translated request.

        Returns:
            The parsed :class:`AgentResponse`.

        Raises:
            GatewayTransportError: 5xx, 429, 408, connect refused,
                any httpx timeout (connect/read/write/pool), or a
                transport/protocol fault.
            GatewayChainExhausted: HTTP 200 with
                ``stop_reason="error"`` — the proxy walked its own
                chain and every upstream failed.
            GatewayError: 4xx other than the retryable subset,
                non-JSON body, or response that fails AgentResponse
                validation.
        """
        thinking_config: ThinkingConfig | None = None
        if thinking is not None:
            thinking_config = (
                ThinkingConfig.model_validate(thinking) if isinstance(thinking, dict) else thinking
            )
        request = AgentRequest(
            schema_version="1",
            capability=capability.value,
            system=system,
            messages=list(messages),
            tools=list(tools),
            max_tokens=max_tokens,
            temperature=temperature,
            skip_models=sorted(skip_models),
            thinking=thinking_config,
        )
        body = request.model_dump(exclude_none=True)
        url = f"{self._base_url}/v1/agent"
        headers = {
            "Content-Type": "application/json",
            "X-Api-Key": self._api_key,
            "Authorization": f"Bearer {self._api_key}",
        }

        try:
            with httpx.Client(timeout=httpx.Timeout(self._timeout_s, connect=5.0)) as client:
                resp = client.post(url, json=body, headers=headers)
        except (
            httpx.TimeoutException,
            httpx.TransportError,
            httpx.RemoteProtocolError,
        ) as exc:
            _log.warning(
                "proxy_gateway.transport_failed",
                url=url,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            raise GatewayTransportError(f"proxy unreachable: {exc}") from exc

        if resp.status_code >= 500 or resp.status_code in (408, 429):
            _log.warning(
                "proxy_gateway.transport_status",
                status=resp.status_code,
                body_preview=resp.text[:200],
            )
            raise GatewayTransportError(f"proxy returned {resp.status_code}: {resp.text[:200]}")

        if resp.status_code >= 400:
            _log.warning(
                "proxy_gateway.client_error",
                status=resp.status_code,
                body_preview=resp.text[:200],
            )
            raise GatewayError(f"proxy returned {resp.status_code}: {resp.text[:200]}")

        try:
            payload = resp.json()
        except json.JSONDecodeError as exc:
            raise GatewayError(f"proxy returned non-JSON body: {exc}") from exc

        try:
            response = AgentResponse.model_validate(payload)
        except Exception as exc:
            raise GatewayError(f"proxy response failed AgentResponse validation: {exc}") from exc

        if response.stop_reason == "error":
            err = response.error
            code = err.code if err else "unknown"
            message = err.message if err else ""
            _log.warning(
                "proxy_gateway.chain_exhausted",
                error_code=code,
                error_message=message[:200],
            )
            raise GatewayChainExhausted(f"gateway error {code!r}: {message!r}")

        _log.debug(
            "proxy_gateway.call_ok",
            capability=capability.value,
            n_messages=len(messages),
            stop_reason=response.stop_reason,
            model_used=response.model_used,
        )
        return response


__all__ = [
    "GatewayChainExhausted",
    "GatewayError",
    "GatewayTransportError",
    "ProxyGatewayClient",
]
