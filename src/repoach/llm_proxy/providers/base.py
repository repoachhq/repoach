"""Base provider interface - extend this to implement your own provider."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

import httpx
from pydantic import BaseModel

from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter


class ProviderConfig(BaseModel):
    """Configuration for a provider.

    Base fields apply to all providers. Provider-specific parameters
    (e.g. NIM temperature, top_p) are passed by the provider constructor.
    """

    api_key: str
    base_url: str | None = None
    rate_limit: int | None = None
    rate_window: int = 60
    max_concurrency: int = 5
    rate_limit_block_default_s: float = 60.0
    rate_limit_block_max_s: float = 300.0
    http_read_timeout: float = 300.0
    http_write_timeout: float = 10.0
    http_connect_timeout: float = 2.0
    enable_thinking: bool = True
    log_full_content: bool = False
    proxy: str = ""


class BaseProvider(ABC):
    """Base class for all providers. Extend this to add your own.

    Every provider is a first-class member of the one configured chain.
    A provider that cannot forward Anthropic ``tools`` natively (e.g.
    ``claude_code``) translates them into a system-prompt appendix and
    parses the model's text back into ``tool_use`` blocks — so it serves
    tool requests transparently as the chain's last-resort backstop.
    """

    _global_rate_limiter: GlobalRateLimiter | None = None

    def __init__(self, config: ProviderConfig):
        self._config = config

    @property
    def rate_limiter(self) -> GlobalRateLimiter | None:
        """Return this provider's process-wide rate limiter scope, if any.

        Every concrete provider that talks to a real upstream sets
        ``self._global_rate_limiter`` in its constructor (via
        :meth:`_scoped_rate_limiter`, or the equivalent direct
        ``GlobalRateLimiter.get_scoped_instance`` call made by
        ``claude_code``). Exposed here so the failover dispatcher can
        check whether a provider is currently blocked BEFORE
        dispatching to it, without reaching into a concrete subclass's
        private attribute (SP-FAILOVER-SKIPS-BLOCKED-PROVIDER). A test
        double that never talks to a real upstream is free to leave
        this unset ; the class-level default of ``None`` means "never
        blocked" to every caller.
        """
        return self._global_rate_limiter

    def _build_timeout(self) -> httpx.Timeout:
        """Build the httpx.Timeout derived from this provider's ProviderConfig.

        Maps ``http_read_timeout`` to both the overall timeout and the
        ``read`` timeout, matching every transport's existing timeout shape.
        """
        return httpx.Timeout(
            self._config.http_read_timeout,
            connect=self._config.http_connect_timeout,
            read=self._config.http_read_timeout,
            write=self._config.http_write_timeout,
        )

    def _scoped_rate_limiter(self, provider_name: str) -> GlobalRateLimiter:
        """Return the process-wide rate limiter scoped to ``provider_name``.

        Delegates to ``GlobalRateLimiter.get_scoped_instance`` with the
        scope lower-cased and the concurrency/window/limit fields read
        from this provider's ``ProviderConfig``.
        """
        return GlobalRateLimiter.get_scoped_instance(
            provider_name.lower(),
            rate_limit=self._config.rate_limit,
            rate_window=self._config.rate_window,
            max_concurrency=self._config.max_concurrency,
        )

    def _is_thinking_enabled(self, request: Any) -> bool:
        """Return whether thinking should be enabled for this request."""
        thinking = getattr(request, "thinking", None)
        request_enabled = True
        if thinking is not None:
            thinking_type = (
                thinking.get("type")
                if isinstance(thinking, dict)
                else getattr(thinking, "type", None)
            )
            if thinking_type == "disabled":
                request_enabled = False

            enabled = (
                thinking.get("enabled")
                if isinstance(thinking, dict)
                else getattr(thinking, "enabled", None)
            )
            if enabled is not None:
                request_enabled = bool(enabled)
        return self._config.enable_thinking and request_enabled

    @abstractmethod
    async def cleanup(self) -> None:
        """Release any resources held by this provider."""

    @abstractmethod
    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Stream response in Anthropic SSE format.

        The ``yield`` below is unreachable but required for ty /
        mypy to accept this method as an abstract async generator.
        """
        if False:
            yield ""
