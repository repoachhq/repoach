"""Tests for the probe-matrix sweep (SP-CHAINPILOT-PROBE-SWEEP, Phase 2a-2).

Covers the 1:1 fan-out over a matrix, the no-credential cell (no HTTP), the
concurrency bound (peak in-flight never exceeds max_concurrency), and the
empty / all-error never-raises guarantees — all without live network.
"""

from __future__ import annotations

import httpx
import pytest

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.cell_probe_sweep import sweep_cell_health
from repoach.llm_proxy.providers.model_catalog import ListedModel, ProviderModelListing
from repoach.llm_proxy.providers.model_matrix import assemble_matrix

_OK_BODY = {"choices": [{"message": {"content": "ok"}}]}


def _ok(provider_id: str, *model_ids: str) -> ProviderModelListing:
    return ProviderModelListing(provider_id, tuple(ListedModel(m) for m in model_ids), True, "")


def _descriptors(*provider_ids: str) -> dict:
    return {pid: PROVIDER_DESCRIPTORS[pid] for pid in provider_ids}


async def test_one_health_per_cell_in_order(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([_ok("nvidia_nim", "a", "b", "c")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await sweep_cell_health(
            matrix, Settings(_env_file=None), client, descriptors=_descriptors("nvidia_nim")
        )

    assert [(r.provider_id, r.model_id) for r in results] == [
        ("nvidia_nim", "a"),
        ("nvidia_nim", "b"),
        ("nvidia_nim", "c"),
    ]
    assert all(r.status == "ok" for r in results)


async def test_unkeyed_provider_cells_are_no_credential_without_http(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")
    for var in ("DEEPSEEK_API_KEY", "REPOACH_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "deepseek" not in str(request.url)
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([_ok("nvidia_nim", "a"), _ok("deepseek", "d1", "d2")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await sweep_cell_health(
            matrix,
            Settings(_env_file=None),
            client,
            descriptors=_descriptors("nvidia_nim", "deepseek"),
        )

    by_model = {r.model_id: r for r in results}
    assert by_model["a"].status == "ok"
    assert by_model["d1"].status == "error"
    assert by_model["d1"].detail == "no credential"
    assert by_model["d1"].latency_s is None


async def test_concurrency_never_exceeds_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")
    state = {"in_flight": 0, "peak": 0}

    async def handler(request: httpx.Request) -> httpx.Response:
        import asyncio

        state["in_flight"] += 1
        state["peak"] = max(state["peak"], state["in_flight"])
        await asyncio.sleep(0.02)
        state["in_flight"] -= 1
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([_ok("nvidia_nim", *[f"m{i}" for i in range(8)])])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await sweep_cell_health(
            matrix,
            Settings(_env_file=None),
            client,
            descriptors=_descriptors("nvidia_nim"),
            max_concurrency=2,
        )

    assert len(results) == 8
    assert state["peak"] <= 2
    assert state["peak"] >= 1


async def test_empty_matrix_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await sweep_cell_health(
            matrix, Settings(_env_file=None), client, descriptors=_descriptors("nvidia_nim")
        )

    assert results == []


async def test_all_cells_error_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    matrix = assemble_matrix([_ok("nvidia_nim", "a", "b")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await sweep_cell_health(
            matrix, Settings(_env_file=None), client, descriptors=_descriptors("nvidia_nim")
        )

    assert len(results) == 2
    assert all(r.status == "error" for r in results)


async def test_pacing_s_delays_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """With pacing_s > 0, elapsed wall-clock time reflects the inter-probe delay.

    A small matrix of 3 cells probed with max_concurrency=1 and pacing_s=0.05
    should take at least 2 * 0.05 = 0.10 s of cumulative pacing (the first
    probe has no predecessor delay, the remaining two each wait). The measured
    elapsed time must be >= 0.08 s to allow for timer jitter.
    """
    import time

    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_OK_BODY)

    matrix = assemble_matrix([_ok("nvidia_nim", "a", "b", "c")])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        t0 = time.monotonic()
        results = await sweep_cell_health(
            matrix,
            Settings(_env_file=None),
            client,
            descriptors=_descriptors("nvidia_nim"),
            max_concurrency=1,
            pacing_s=0.05,
        )
        elapsed = time.monotonic() - t0

    assert len(results) == 3
    assert all(r.status == "ok" for r in results)
    assert elapsed >= 0.08, f"expected >= 0.08 s pacing delay, got {elapsed:.3f} s"
