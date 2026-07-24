"""SP-CONSISTENCY-SWEEP G1 — ``AgentLoop`` no longer pins dead attributes.

Audit 2026-07-13 finding C1: ``AgentLoop.__init__`` assigned
``self._base_url`` / ``self._api_key`` after building ``self._client``
— neither was ever read again, and ``self._api_key`` re-pinned the raw
bearer token (unwrapped from its ``SecretStr``) on a second long-lived
object, defeating the secret hygiene the rest of the path maintains.
Both assignments are removed; the proxy client built from the resolved
base URL is the only place the token/base-url pair now lives.
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
        settings.llm_proxy_base_url = "http://localhost:8082"
        settings.llm_proxy_auth_token = _MockSecret("test-token")
        mock.return_value = settings
        yield settings


def test_base_url_and_api_key_not_pinned(mock_settings) -> None:
    """Constructing an ``AgentLoop`` no longer sets the dead attributes.

    Pre-fix, both ``hasattr`` checks below were ``True`` (the dead
    assignments pinned the raw token on the loop instance). Post-fix
    they are absent, and the proxy client is still built from the
    resolved ``base_url`` — the only carrier of that data now.
    """
    loop = AgentLoop(capability=CapabilityTier.SONNET)

    assert not hasattr(loop, "_base_url")
    assert not hasattr(loop, "_api_key")
    assert loop._client._base_url == "http://localhost:8082"
    assert loop._client._api_key == "test-token"
