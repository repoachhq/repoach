"""Tests for SP-CHAINPILOT-MATRIX (Phase 1b).

Covers the pure flattener (cells from ok listings, provenance preserved) and
the async sweep orchestrator: sweepable filtering, graceful no-credential skip,
and the claude_code exclusion — all without live network via httpx.MockTransport.
"""

from __future__ import annotations

import httpx
import pytest

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.catalog import PROVIDER_DESCRIPTORS
from repoach.llm_proxy.providers.model_catalog import ListedModel, ProviderModelListing
from repoach.llm_proxy.providers.model_matrix import (
    ModelCell,
    assemble_matrix,
    sweep_model_matrix,
)


def _ok(provider_id: str, *model_ids: str) -> ProviderModelListing:
    models = tuple(ListedModel(m) for m in model_ids)
    return ProviderModelListing(provider_id, models, True, f"{len(models)} models")


def test_assemble_flattens_ok_listings() -> None:
    matrix = assemble_matrix([_ok("nvidia_nim", "a", "b", "c"), _ok("kimi", "x", "y")])

    assert len(matrix) == 5
    assert matrix.providers() == ("nvidia_nim", "kimi")
    assert matrix.models_for("nvidia_nim") == ("a", "b", "c")
    assert matrix.models_for("kimi") == ("x", "y")
    assert ModelCell("kimi", "x") in matrix.cells


def test_assemble_preserves_failed_listing_without_cells() -> None:
    failed = ProviderModelListing("deepseek", (), False, "http=410")
    matrix = assemble_matrix([_ok("groq", "g1"), failed])

    assert len(matrix) == 1
    assert matrix.models_for("deepseek") == ()
    assert matrix.models_for("unknown") == ()
    assert failed in matrix.listings


async def test_sweep_lists_keyed_and_skips_unkeyed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")
    for var in ("DEEPSEEK_API_KEY", "REPOACH_DEEPSEEK_API_KEY"):
        monkeypatch.delenv(var, raising=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "deepseek" not in str(request.url)
        return httpx.Response(200, json={"data": [{"id": "m1"}, {"id": "m2"}]})

    descriptors = {pid: PROVIDER_DESCRIPTORS[pid] for pid in ("nvidia_nim", "deepseek")}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        matrix = await sweep_model_matrix(Settings(_env_file=None), client, descriptors=descriptors)

    assert matrix.models_for("nvidia_nim") == ("m1", "m2")
    by_provider = {listing.provider_id: listing for listing in matrix.listings}
    assert by_provider["deepseek"].ok is False
    assert by_provider["deepseek"].detail == "no credential"
    assert matrix.models_for("deepseek") == ()


async def test_sweep_excludes_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_NVIDIA_NIM_API_KEY", "nim-key")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "only"}]})

    descriptors = {pid: PROVIDER_DESCRIPTORS[pid] for pid in ("nvidia_nim", "claude_code")}
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        matrix = await sweep_model_matrix(Settings(_env_file=None), client, descriptors=descriptors)

    provider_ids = {listing.provider_id for listing in matrix.listings}
    assert "claude_code" not in provider_ids
    assert all(cell.provider_id != "claude_code" for cell in matrix.cells)
