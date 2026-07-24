"""Unit tests for SP-CONFIG-ENV-ANCHOR's ``AgentLoop`` half (G3).

``agent_loop.py:332`` used to duplicate the retired ``:8082`` literal as
an ``or`` fallback — a second hardcoded default surviving any future fix
to ``Settings``. Pins that the call site now reads
``settings.llm_proxy_base_url`` verbatim, with no fallback of its own.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from repoach.agent_engine.agent_loop import AgentLoop
from repoach.llm.capability import CapabilityTier


class _MockSecret:
    def __init__(self, value: str) -> None:
        self._value = value

    def get_secret_value(self) -> str:
        return self._value


@pytest.fixture
def mock_settings():
    with patch("repoach.agent_engine.agent_loop.get_settings") as mock:
        settings = MagicMock()
        settings.llm_proxy_base_url = "http://sentinel-host:9999"
        settings.llm_proxy_auth_token = _MockSecret("test-token")
        mock.return_value = settings
        yield settings


def test_base_url_has_no_hardcoded_fallback(mock_settings) -> None:
    """The client targets whatever ``settings.llm_proxy_base_url`` says.

    A sentinel host that has nothing to do with either ``:8082`` or
    ``:8084`` proves the call site reads the resolved value verbatim
    rather than silently substituting a literal of its own.
    """
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    assert loop._base_url == "http://sentinel-host:9999/v1"


def test_base_url_never_falls_back_to_the_retired_8082_default(mock_settings) -> None:
    """A falsy resolved URL must NOT be coerced to the old ``:8082`` literal.

    Pre-fix: ``(settings.llm_proxy_base_url or "http://localhost:8082")``
    turned any falsy value into the hardcoded default. Post-fix there is
    no ``or`` fallback left at the call site, so a falsy value passes
    through unchanged instead of silently becoming ``:8082``.
    """
    mock_settings.llm_proxy_base_url = ""
    loop = AgentLoop(capability=CapabilityTier.SONNET)
    assert "8082" not in loop._base_url
    assert loop._base_url == "/v1"
