"""Tests for the RoutingTable (SP-ROUTING-TABLE / SP-ROUTING-SWITCHOVER).

Characterises ``RoutingTable.chain_for`` — the single chain resolver that
replaced ``Settings.resolve_models`` at the switchover. The ``settings``
fixture builds a deterministic Settings from explicit bare ``MODEL_*``
env vars with the dotenv files disabled, so the real ``chains.env``
cannot leak into the assertions.
"""

from __future__ import annotations

import pytest

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.routing.table import RoutingTable
from repoach.llm_proxy.routing.tier import Tier

_ENV_KEYS = [
    "MODEL",
    "MODEL_OPUS",
    "MODEL_SONNET",
    "MODEL_HAIKU",
    "REPOACH_PROXY_DEFAULT_MODEL",
    "REPOACH_MODEL_OPUS",
    "REPOACH_MODEL_SONNET",
    "REPOACH_MODEL_HAIKU",
]


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in _ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/default/model")
    monkeypatch.setenv("MODEL_OPUS", "nvidia_nim/opus-1,claude_code/opus")
    monkeypatch.setenv("MODEL_SONNET", "open_router/sonnet-1")
    return Settings(_env_file=None)


def _refs(table: RoutingTable, name: str) -> list[str]:
    return [str(ref) for ref in table.chain_for(name).refs]


def test_from_settings_routes_configured_tiers(settings: Settings) -> None:
    table = RoutingTable.from_settings(settings)
    assert _refs(table, "claude-opus-4-7") == ["nvidia_nim/opus-1", "claude_code/opus"]
    assert _refs(table, "claude-sonnet-4") == ["open_router/sonnet-1"]
    assert _refs(table, "claude-coder-x") == ["nvidia_nim/default/model"]


def test_unconfigured_tier_falls_back_to_default(settings: Settings) -> None:
    table = RoutingTable.from_settings(settings)
    assert Tier.HAIKU not in table.chains
    assert _refs(table, "claude-3-5-haiku") == ["nvidia_nim/default/model"]
    assert _refs(table, "gpt-4o") == ["nvidia_nim/default/model"]


def test_explicit_provider_ref_passes_through(settings: Settings) -> None:
    table = RoutingTable.from_settings(settings)
    assert _refs(table, "nvidia_nim/some/model") == ["nvidia_nim/some/model"]
    assert _refs(table, "open_router/x") == ["open_router/x"]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("claude-opus-4-7", ["nvidia_nim/opus-1", "claude_code/opus"]),
        ("claude-sonnet-4", ["open_router/sonnet-1"]),
        ("claude-3-5-haiku", ["nvidia_nim/default/model"]),
        ("claude-coder-x", ["nvidia_nim/default/model"]),
        ("gpt-4o", ["nvidia_nim/default/model"]),
        ("nvidia_nim/some/model", ["nvidia_nim/some/model"]),
        ("open_router/x", ["open_router/x"]),
    ],
)
def test_chain_for_resolves_each_name(settings: Settings, name: str, expected: list[str]) -> None:
    table = RoutingTable.from_settings(settings)
    assert _refs(table, name) == expected
