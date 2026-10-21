"""In-process direct providers: the gateway's own transports, no gateway (SP-UNIVERSAL-PROVIDERS).

A direct provider runs one of the gateway's upstream transports inside
the review process — :class:`~repoach.llm_proxy.providers.claude_code.ClaudeCodeProvider`
(the logged-in ``claude`` CLI, headless),
:class:`~repoach.llm_proxy.providers.anthropic_messages.AnthropicMessagesTransport`
(the Messages API with a key) or
:class:`~repoach.llm_proxy.providers.openai_generic.GenericOpenAIProvider`
(any ``/chat/completions`` endpoint: OpenAI, Ollama, OpenRouter, Groq,
DeepSeek, Mistral, Gemini's compatible surface) — and translates with
the gateway dispatcher's public helpers, so there is one implementation
of each upstream, not two.

Every direct call runs on one event loop owned by one daemon thread
(:class:`_LoopThread`).  The transports hold asyncio primitives (the
scoped rate limiter, an ``httpx.AsyncClient``) that must never be shared
between loops, and the reviewers call from four threads at once; one
loop gives them the concurrency semantics they have inside the gateway.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import threading
import time
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from ..core.logging import get_logger
from ..llm.capability import CapabilityTier
from ..llm_proxy.api.agent_dispatcher import (
    ANTHROPIC_TO_AGENT_STOP,
    aggregate_sse_stream,
    translate_content_blocks,
    translate_request,
)
from ..llm_proxy.api.models.agent_v1 import (
    AgentResponse,
    Message,
    ToolSpec,
    TraceEntry,
)
from ..llm_proxy.api.models.anthropic import ThinkingConfig
from ..llm_proxy.providers.base import BaseProvider
from ..llm_proxy.providers.exceptions import ProviderError
from ..llm_proxy.providers.openai_generic import GenericOpenAIProvider
from .errors import GatewayError, GatewayTransportError, ProviderAuthError, ProviderRequestError
from .protocol import agent_request

_log = get_logger(__name__)

T = TypeVar("T")

CANCEL_GRACE_S = 10.0
"""How long an interrupted call waits for its transport to kill the child process."""

DEADLINE_HEADROOM_S = 30.0
"""Added to the per-call timeout before the caller stops waiting on the loop."""

RETRYABLE_STATUSES: frozenset[int] = frozenset({408, 429, 529})
"""Upstream statuses below 500 that are still transport-level (retried)."""

AUTH_STATUSES: frozenset[int] = frozenset({401, 403})

REQUEST_STATUSES: frozenset[int] = frozenset({400, 404, 422})
"""Statuses meaning the request itself is wrong: never retried (:class:`ProviderRequestError`)."""

CLI_AUTH_MARKERS: tuple[str, ...] = (
    "/login",
    "invalid api key",
    "not logged in",
    "oauth token",
    "authentication",
)
"""Fragments of a ``claude`` CLI failure that mean the login is missing or expired."""


class MeteredChatTransport(GenericOpenAIProvider):
    """The gateway's chat-completions transport, asking the upstream for its token counts.

    A streamed ``/chat/completions`` answer carries ``usage`` only when
    the request sets ``stream_options.include_usage``; the gateway never
    sets it (its token figures are estimates), the in-process route
    does, so the usage ledger records the upstream's own counts.  Every
    other part of the request is the gateway transport's.
    """

    def _build_request_body(self, request: Any) -> dict:
        """Return the gateway transport's body plus ``stream_options.include_usage``."""
        body = super()._build_request_body(request)
        body["stream_options"] = {"include_usage": True}
        return body


class _LoopThread:
    """One daemon thread running the event loop every direct call shares."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def loop(self) -> asyncio.AbstractEventLoop:
        """Return the running loop, starting its thread on first use."""
        with self._lock:
            if self._loop is None:
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=loop.run_forever, name="repoach-direct-provider", daemon=True
                )
                thread.start()
                self._loop = loop
            return self._loop

    def run(self, coro: Coroutine[Any, Any, T], *, timeout_s: float) -> T:
        """Run ``coro`` on the shared loop and wait for it.

        On a timeout or an interrupt of the waiting thread the coroutine
        is cancelled and given :data:`CANCEL_GRACE_S` to clean up (the
        ``claude_code`` transport kills its process group there).

        Raises:
            TimeoutError: ``coro`` did not finish within ``timeout_s``.
        """
        finished = threading.Event()

        async def guarded() -> T:
            try:
                return await coro
            finally:
                finished.set()

        future = asyncio.run_coroutine_threadsafe(guarded(), self.loop())
        try:
            return future.result(timeout=timeout_s)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            finished.wait(CANCEL_GRACE_S)
            raise TimeoutError(f"no answer within {timeout_s:.0f}s") from exc
        except BaseException:
            future.cancel()
            finished.wait(CANCEL_GRACE_S)
            raise


_LOOP = _LoopThread()

_TRANSPORTS: dict[tuple[str, ...], BaseProvider] = {}
"""Transports built so far, by :attr:`DirectProvider.transport_key`; owned by the loop thread."""


async def _transport(key: tuple[str, ...], factory: Callable[[], BaseProvider]) -> BaseProvider:
    """Return the transport for ``key``, building it on the loop thread on first use."""
    transport = _TRANSPORTS.get(key)
    if transport is None:
        transport = factory()
        _TRANSPORTS[key] = transport
    return transport


class DirectProvider:
    """A :class:`~repoach.providers.protocol.Provider` over an in-process gateway transport.

    Attributes:
        name: ``claude_code``, ``anthropic``, ``gemini``, ``openai_compatible``
            or the preset that set its base URL (``ollama``, ``openrouter``…).
        models: Model per capability tier, as sent to the transport.
        transport_key: Identity of the transport configuration; providers
            sharing it share one transport (one rate limiter, one client).
        base_url: The API root the transport posts to (``""`` for the
            CLI); the usage ledger prices a call by its host.
    """

    def __init__(
        self,
        *,
        name: str,
        models: dict[CapabilityTier, str],
        transport_key: tuple[str, ...],
        transport_factory: Callable[[], BaseProvider],
        timeout_s: float,
        auth_hint: str,
        base_url: str = "",
    ) -> None:
        """Initialise the provider; nothing is spawned or connected yet.

        Args:
            name: Provider name, used in ``model_used`` and error messages.
            models: Model per capability tier.
            transport_key: See the class attributes.
            transport_factory: Builds the gateway transport; called once
                per ``transport_key`` on the loop thread.
            timeout_s: Per-call budget; the caller stops waiting after it
                plus :data:`DEADLINE_HEADROOM_S`.
            auth_hint: What to do when the credential is rejected, named
                by variable or command, never by value.
            base_url: See the class attributes.
        """
        self.name = name
        self.models = dict(models)
        self.transport_key = transport_key
        self._factory = transport_factory
        self._timeout_s = timeout_s
        self._auth_hint = auth_hint
        self.base_url = base_url

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
        """Run one turn through the transport; see :class:`~repoach.providers.protocol.Provider`.

        Raises:
            GatewayTransportError: Timeout, unreachable upstream, 5xx,
                408/429/529, or a ``claude`` CLI failure that is not a
                login problem.
            GatewayError: Rejected credential (401/403, CLI not logged
                in) or another 4xx; the message names what to fix.
        """
        model = self.models[capability]
        request = agent_request(
            capability=capability,
            system=system,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            temperature=temperature,
            skip_models=frozenset(),
            thinking=thinking,
        )
        messages_request = translate_request(request, model)
        started = time.monotonic()
        try:
            aggregated = _LOOP.run(
                self._stream(messages_request), timeout_s=self._timeout_s + DEADLINE_HEADROOM_S
            )
        except TimeoutError as exc:
            raise GatewayTransportError(f"{self.name}: {exc}") from exc
        except ProviderError as exc:
            raise self._cli_error(exc) from exc
        elapsed_ms = int((time.monotonic() - started) * 1000)
        error = aggregated.get("error")
        if error is not None:
            raise self._status_error(error.get("status_code"), str(error.get("message") or ""))
        model_used = f"{self.name}/{model}"
        usage = aggregated["usage"]
        stop_reason = ANTHROPIC_TO_AGENT_STOP.get(aggregated.get("stop_reason") or "", "end_turn")
        response = AgentResponse(
            schema_version="1",
            capability=capability.value,
            stop_reason=stop_reason,
            model_used=model_used,
            content=translate_content_blocks(aggregated["content_blocks"]),
            usage=usage,
            elapsed_ms=elapsed_ms,
            trace=[TraceEntry(model=model_used, outcome="ok", elapsed_ms=elapsed_ms, usage=usage)],
        )
        _log.debug(
            "direct_provider.call_ok",
            provider=self.name,
            model=model,
            stop_reason=stop_reason,
            elapsed_ms=elapsed_ms,
        )
        return response

    def _stream(self, messages_request: Any) -> Coroutine[Any, Any, dict[str, Any]]:
        """Return the coroutine that streams one request and aggregates it."""

        async def run() -> dict[str, Any]:
            transport = await _transport(self.transport_key, self._factory)
            return await aggregate_sse_stream(transport.stream_response(messages_request))

        return run()

    def _status_error(self, status: int | None, message: str) -> GatewayError:
        """Map an in-band upstream error event to the loop's error types."""
        detail = f"{self.name}: upstream error"
        if status is not None:
            detail += f" {status}"
        detail += f": {message[:300]}"
        if status in AUTH_STATUSES:
            return ProviderAuthError(f"{detail} -- {self._auth_hint}")
        if status in REQUEST_STATUSES:
            return ProviderRequestError(f"{detail} -- check the model id and the endpoint")
        if status is None or status >= 500 or status in RETRYABLE_STATUSES:
            return GatewayTransportError(detail)
        return GatewayError(detail)

    def _cli_error(self, exc: ProviderError) -> GatewayError:
        """Map a raised transport error; a login problem is never retried."""
        text = str(exc)
        if exc.status_code in AUTH_STATUSES or any(m in text.lower() for m in CLI_AUTH_MARKERS):
            return ProviderAuthError(f"{self.name}: {text[:300]} -- {self._auth_hint}")
        return GatewayTransportError(f"{self.name}: {text[:300]}")


__all__ = ["DirectProvider", "MeteredChatTransport"]
