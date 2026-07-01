"""Base provider interface - extend this to implement your own provider."""

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any

from pydantic import BaseModel


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
    http_read_timeout: float = 300.0
    http_write_timeout: float = 10.0
    http_connect_timeout: float = 2.0
    enable_thinking: bool = True
    proxy: str = ""


class BaseProvider(ABC):
    """Base class for all providers. Extend this to add your own.

    Every provider is a first-class member of the one configured chain.
    A provider that cannot forward Anthropic ``tools`` natively (e.g.
    ``claude_code``) translates them into a system-prompt appendix and
    parses the model's text back into ``tool_use`` blocks — so it serves
    tool requests transparently as the chain's last-resort backstop.
    """

    def __init__(self, config: ProviderConfig):
        self._config = config

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
