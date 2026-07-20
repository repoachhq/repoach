"""Tests for the effort-aware probe sweep (SP-CHAINPILOT-EFFORT-SWEEP, 2a-3-ii).

Covers per-provider effort application on the wire, the 1:1 matrix order, the
effort_used / model_used tagging, the no-credential and never-raises guarantees —
all without live network.
"""

from __future__ import annotations

import json

import httpx
import pytest

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.effort_sweep import sweep_effort_health
from repoach.llm_proxy.providers.model_catalog import ListedModel, ProviderModelListing
from repoach.llm_proxy.providers.model_matrix import assemble_matrix

_OK_BODY = {"choices": [{"message": {"content": "ok"}}]}


def _ok(provider_id: str, *model_ids: str) -> ProviderModelListing:
    return ProviderModelListing(provider_id, tuple(ListedModel(m) for m in model_ids), True, "")


def _descriptors(*provider_ids: str) -> dict:
    return {pid: PROVIDER_DESCRIPTORS[pid] for pid in provider_ids}


def _capturing_handler(captured: list[tuple[str, dict]]):
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append((body["model"], body))
        return httpx.Response(200, json=_OK_BODY)

    return handler


async def test_effort_applied_per_provider_on_the_wire(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_GROQ_API_KEY", "groq-key")
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")
    captured: list[tuple[str, dict]] = []

    matrix = assemble_matrix([_ok("groq", "g1"), _ok("nvidia_nim", "n1")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(_capturing_handler(captured))) as c:
        probes = await sweep_effort_health(
            matrix, Settings(_env_file=None), c, descriptors=_descriptors("groq", "nvidia_nim")
        )

    bodies = dict(captured)
    assert bodies["g1"].get("reasoning_effort") == "low"
    assert "reasoning_effort" not in bodies["n1"]

    by_model = {p.health.model_id: p for p in probes}
    assert [(p.health.provider_id, p.health.model_id) for p in probes] == [
        ("groq", "g1"),
        ("nvidia_nim", "n1"),
    ]
    assert by_model["g1"].effort_used == "low"
    assert by_model["n1"].effort_used is None
    assert all(p.model_used == p.health.model_id for p in probes)


async def test_deepseek_high_kimi_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("REPOACH_KIMI_API_KEY", "kimi-key")
    captured: list[tuple[str, dict]] = []

    matrix = assemble_matrix([_ok("deepseek", "d1"), _ok("kimi", "k1")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(_capturing_handler(captured))) as c:
        probes = await sweep_effort_health(
            matrix, Settings(_env_file=None), c, descriptors=_descriptors("deepseek", "kimi")
        )

    bodies = dict(captured)
    assert bodies["d1"].get("reasoning_effort") == "high"
    assert "reasoning_effort" not in bodies["k1"]

    by_model = {p.health.model_id: p for p in probes}
    assert by_model["d1"].effort_used == "high"
    assert by_model["k1"].effort_used is None


async def test_no_credential_cell_wrapped_with_effort_no_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")
    for var in ("DEEPSEEK_API_KEY", "REPOACH_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "deepseek" not in str(request.url)
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([_ok("nvidia_nim", "n1"), _ok("deepseek", "d1")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        probes = await sweep_effort_health(
            matrix, Settings(_env_file=None), c, descriptors=_descriptors("nvidia_nim", "deepseek")
        )

    by_model = {p.health.model_id: p for p in probes}
    assert by_model["d1"].health.status == "error"
    assert by_model["d1"].health.detail == "no credential"
    assert by_model["d1"].effort_used == "high"
    assert by_model["d1"].model_used == "d1"


async def test_never_raises_on_dead_cell(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_GROQ_API_KEY", "groq-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    matrix = assemble_matrix([_ok("groq", "g1", "g2")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        probes = await sweep_effort_health(
            matrix, Settings(_env_file=None), c, descriptors=_descriptors("groq")
        )

    assert len(probes) == 2
    assert all(p.health.status == "error" for p in probes)
    assert all(p.effort_used == "low" for p in probes)


async def test_empty_matrix_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_GROQ_API_KEY", "groq-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as c:
        probes = await sweep_effort_health(
            matrix, Settings(_env_file=None), c, descriptors=_descriptors("groq")
        )

    assert probes == []
