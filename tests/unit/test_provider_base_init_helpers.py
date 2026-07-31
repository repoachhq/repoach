"""Tests for SP-PROVIDER-INIT-DEDUP.

Covers the shared ``BaseProvider._build_timeout`` /
``BaseProvider._scoped_rate_limiter`` helpers that fold the previously
duplicated transport-init skeleton (the ``GlobalRateLimiter`` scoped lookup
and the ``httpx.Timeout`` construction) out of ``OpenAIChatTransport`` and
``AnthropicMessagesTransport``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import httpx

from repoach.llm_proxy.providers.base import BaseProvider, ProviderConfig
from repoach.llm_proxy.providers.open_router.client import OpenRouterProvider
from repoach.llm_proxy.providers.openai_generic import GenericOpenAIProvider
from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter


class _MinimalProvider(BaseProvider):
    """The smallest concrete BaseProvider subclass, for exercising helpers.

    ``BaseProvider`` is abstract, so a minimal concrete subclass is needed
    to instantiate it and call the helpers under test directly.
    """

    async def cleanup(self) -> None:
        """No resources to release."""

    async def stream_response(
        self,
        request: Any,
        input_tokens: int = 0,
        *,
        request_id: str | None = None,
    ) -> AsyncIterator[str]:
        """Unused stub required by the abstract interface."""
        if False:
            yield ""


def test_build_timeout_maps_config_fields() -> None:
    config = ProviderConfig(
        api_key="test-key",
        http_read_timeout=123.0,
        http_connect_timeout=7.0,
        http_write_timeout=11.0,
    )
    provider = _MinimalProvider(config)

    timeout = provider._build_timeout()

    assert isinstance(timeout, httpx.Timeout)
    assert timeout.read == config.http_read_timeout
    assert timeout.connect == config.http_connect_timeout
    assert timeout.write == config.http_write_timeout


def test_scoped_rate_limiter_matches_direct_call() -> None:
    config = ProviderConfig(
        api_key="test-key",
        rate_limit=17,
        rate_window=42,
        max_concurrency=3,
    )
    provider = _MinimalProvider(config)

    limiter = provider._scoped_rate_limiter("SP-PROVIDER-INIT-DEDUP-NIM")

    direct = GlobalRateLimiter.get_scoped_instance(
        "sp-provider-init-dedup-nim",
        rate_limit=config.rate_limit,
        rate_window=config.rate_window,
        max_concurrency=config.max_concurrency,
    )

    assert limiter is direct


def test_both_transports_route_init_through_base_helpers() -> None:
    config = ProviderConfig(api_key="test-key")

    with (
        patch.object(
            BaseProvider,
            "_build_timeout",
            autospec=True,
            wraps=BaseProvider._build_timeout,
        ) as timeout_spy,
        patch.object(
            BaseProvider,
            "_scoped_rate_limiter",
            autospec=True,
            wraps=BaseProvider._scoped_rate_limiter,
        ) as limiter_spy,
    ):
        openai_provider = GenericOpenAIProvider(
            config, provider_name="sp-provider-init-dedup-openai"
        )
        anthropic_provider = OpenRouterProvider(config)

    assert timeout_spy.call_count >= 2
    assert limiter_spy.call_count >= 2

    assert openai_provider._global_rate_limiter is not None
    assert anthropic_provider._global_rate_limiter is not None
