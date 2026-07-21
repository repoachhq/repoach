"""Unit suite for the model-first live gather + regenerate entrypoint (SP-MFC-REGEN).

The gather functions are monkeypatched in the module namespace, so no test
performs live network or DB I/O. Covers the latency reduction, the compose, the
apply gate, and the CLI command registration.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import httpx
import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.health.credits import reset_credits_cache
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.aa_ingest import (
    AaRanking,
    ModelCapability,
    normalize_model_name,
)
from repoach.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from repoach.llm_proxy.providers.cell_probe_store import CellProbeRow
from repoach.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from repoach.llm_proxy.routing import chain_regen
from repoach.llm_proxy.routing.chain_regen import (
    gather_and_regenerate,
    speed_for_from_rows,
)

_CHAINS = "\n".join(
    [
        "# header",
        "MODEL_OPUS=old/opus",
        "MODEL_SONNET=old/sonnet",
        "MODEL_HAIKU=old/haiku",
        "",
    ]
)


def _row(provider: str, model: str, latency: float | None, *, day: int) -> CellProbeRow:
    """A CellProbeRow stamped on a fixed day (newer = later test list position)."""
    return CellProbeRow(
        recorded_at=datetime(2026, 6, day, tzinfo=UTC),
        provider_id=provider,
        model_id=model,
        status="ok",
        latency_s=latency,
        content_chars=10,
        reasoning_chars=0,
        detail="",
    )


def test_speed_for_from_rows_latest_wins() -> None:
    """The first row per cell (newest-first input) supplies its latency; unseen → None."""
    rows = [
        _row("nvidia_nim", "m", 1.5, day=20),
        _row("nvidia_nim", "m", 9.9, day=10),
        _row("open_router", "n", None, day=20),
    ]
    speed = speed_for_from_rows(rows)
    assert speed("nvidia_nim", "m") == 1.5
    assert speed("open_router", "n") is None
    assert speed("groq", "z") is None


def _cap(display: str, capability: float) -> ModelCapability:
    """A ModelCapability keyed on the normalized display name."""
    return ModelCapability(
        name=normalize_model_name(display),
        capability=capability,
        coding=None,
        cheapest_input=None,
        fastest_tps=None,
        variants=(),
    )


def _ranking() -> AaRanking:
    """Ranking with the three Claude anchors plus an opus-eligible servable model."""
    return AaRanking(
        index_version="v4.1",
        models=(
            _cap("Claude Opus 4.7", 53.5),
            _cap("Claude Sonnet 4.6", 47.2),
            _cap("Claude 4.5 Haiku", 29.6),
            _cap("Alpha Model", 50.0),
        ),
    )


def _matrix() -> ProviderModelMatrix:
    """Matrix serving Alpha Model on NIM."""
    return ProviderModelMatrix(cells=(ModelCell("nvidia_nim", "x/alpha-model"),), listings=())


def _equivalences() -> EquivalenceTable:
    """Maps the Alpha Model name to its id-pattern token."""
    return EquivalenceTable(
        equivalences=(
            ModelEquivalence(
                canonical="alpha", aliases=("Alpha Model",), id_patterns=("alphamodel",)
            ),
        )
    )


def _fresh_row(provider: str, model: str, latency: float | None) -> CellProbeRow:
    """A CellProbeRow stamped at the current instant (always inside the freshness window)."""
    return CellProbeRow(
        recorded_at=datetime.now(UTC),
        provider_id=provider,
        model_id=model,
        status="ok",
        latency_s=latency,
        content_chars=10,
        reasoning_chars=0,
        detail="",
    )


def _patch_gatherers(monkeypatch) -> None:
    """Stub every live gather function in the chain_regen namespace.

    ``fetch_cell_probes`` returns one fresh (now-stamped) row, so the G5
    freshness read this step adds does not change the pre-existing
    shadow/apply tests' outcome.
    """

    async def _fake_sweep(settings, client):
        return _matrix()

    def _fake_fetch_cell_probes(
        db_path, *, since=None, provider_id=None, model_id=None, limit=None
    ):
        return [_fresh_row("nvidia_nim", "x/alpha-model", 1.0)]

    monkeypatch.setattr(chain_regen, "fetch_aa_ranking", lambda settings: _ranking())
    monkeypatch.setattr(chain_regen, "sweep_model_matrix", _fake_sweep)
    monkeypatch.setattr(chain_regen, "load_equivalence_table", lambda: _equivalences())
    monkeypatch.setattr(chain_regen, "fetch_cell_probes", _fake_fetch_cell_probes)


async def test_gather_and_regenerate_shadow_does_not_write(tmp_path, monkeypatch) -> None:
    """A shadow run composes the pipeline but leaves chains.env untouched."""
    _patch_gatherers(monkeypatch)
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    result = await gather_and_regenerate(
        Settings(_env_file=None),
        client=None,
        chains_path=target,
        db_path=tmp_path / "db.sqlite",
        enabled=False,
    )
    assert result.changed is True
    assert result.written is False
    assert target.read_text(encoding="utf-8") == _CHAINS


async def test_gather_and_regenerate_apply_writes(tmp_path, monkeypatch) -> None:
    """An armed run writes the regenerated chains.env."""
    _patch_gatherers(monkeypatch)
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    result = await gather_and_regenerate(
        Settings(_env_file=None),
        client=None,
        chains_path=target,
        db_path=tmp_path / "db.sqlite",
        enabled=True,
    )
    assert result.written is True
    assert "MODEL_OPUS=nvidia_nim/x/alpha-model,claude_code/opus" in target.read_text(
        encoding="utf-8"
    )


def test_regenerate_chains_command_registered() -> None:
    """The regenerate-chains CLI command exists and renders help."""
    result = CliRunner().invoke(app, ["regenerate-chains", "--help"])
    assert result.exit_code == 0
    assert "regenerate" in result.output.lower()


def test_bounded_sweep() -> None:
    """Chain-ref cells win priority within the cap; the cap holds per provider."""
    chain_ref_cells = (
        ModelCell("nvidia_nim", "a"),
        ModelCell("nvidia_nim", "b"),
    )
    candidate_cells = (
        *tuple(ModelCell("nvidia_nim", f"c{i}") for i in range(5)),
        ModelCell("open_router", "x"),
        ModelCell("open_router", "y"),
    )

    bounded = chain_regen._bounded_cell_set(chain_ref_cells, candidate_cells, per_provider_cap=3)

    nim_cells = [cell for cell in bounded if cell.provider_id == "nvidia_nim"]
    open_router_cells = [cell for cell in bounded if cell.provider_id == "open_router"]
    assert nim_cells[:2] == list(chain_ref_cells)
    assert len(nim_cells) == 3
    assert len(open_router_cells) == 2
    assert len(bounded) <= 2 * 3


async def test_429_handling() -> None:
    """A cell 429s twice: retried once, still rate-limited, and dropped; peers persist."""
    calls = {"a": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        model = body["model"]
        if model == "a":
            calls["a"] += 1
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    settings = Settings(_env_file=None, NVIDIA_NIM_API_KEY="test-token")
    cells = (ModelCell("nvidia_nim", "a"), ModelCell("nvidia_nim", "b"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        results = await chain_regen._probe_bounded_cells(
            cells,
            settings,
            client,
            max_concurrency=2,
            pacing_s=0.0,
            retry_backoff_s=0.0,
        )

    assert calls["a"] == 2
    assert [(r.provider_id, r.model_id) for r in results] == [("nvidia_nim", "b")]


async def test_credits_skip() -> None:
    """A below-floor snapshot drops open_router cells; an unavailable snapshot keeps them."""
    cells = (ModelCell("nvidia_nim", "a"), ModelCell("open_router", "x"))
    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-token", CREDITS_FLOOR_USD=1.0)

    def low_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"total_credits": 1.0, "total_usage": 0.5}})

    reset_credits_cache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(low_handler)) as client:
        kept, skipped = await chain_regen._drop_paid_cells_if_low_credits(cells, settings, client)
    assert kept == (ModelCell("nvidia_nim", "a"),)
    assert skipped == 1

    def error_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    reset_credits_cache()
    async with httpx.AsyncClient(transport=httpx.MockTransport(error_handler)) as client:
        kept_none, skipped_none = await chain_regen._drop_paid_cells_if_low_credits(
            cells, settings, client
        )
    assert kept_none == cells
    assert skipped_none == 0


async def test_freshness_refusal(tmp_path, monkeypatch) -> None:
    """Stale rows and zero rows both raise StaleCellsError; chains.env is untouched."""
    _patch_gatherers(monkeypatch)
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    settings = Settings(_env_file=None, REGEN_MAX_CELL_AGE_H=1.0)

    stale_row = _row("nvidia_nim", "x/alpha-model", 1.0, day=1)

    def _fake_fetch_stale(db_path, *, since=None, provider_id=None, model_id=None, limit=None):
        return [stale_row]

    monkeypatch.setattr(chain_regen, "fetch_cell_probes", _fake_fetch_stale)
    with pytest.raises(chain_regen.StaleCellsError):
        await gather_and_regenerate(
            settings,
            client=None,
            chains_path=target,
            db_path=tmp_path / "db.sqlite",
            enabled=True,
        )
    assert target.read_text(encoding="utf-8") == _CHAINS

    def _fake_fetch_empty(db_path, *, since=None, provider_id=None, model_id=None, limit=None):
        return []

    monkeypatch.setattr(chain_regen, "fetch_cell_probes", _fake_fetch_empty)
    with pytest.raises(chain_regen.StaleCellsError):
        await gather_and_regenerate(
            settings,
            client=None,
            chains_path=target,
            db_path=tmp_path / "db2.sqlite",
            enabled=True,
        )
    assert target.read_text(encoding="utf-8") == _CHAINS


async def test_nominal_fresh_sweep_concludes(tmp_path, monkeypatch) -> None:
    """Fresh probe rows let gather_and_regenerate conclude a normal regeneration."""
    _patch_gatherers(monkeypatch)
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")

    fresh_row = CellProbeRow(
        recorded_at=datetime.now(UTC),
        provider_id="nvidia_nim",
        model_id="x/alpha-model",
        status="ok",
        latency_s=1.2,
        content_chars=10,
        reasoning_chars=0,
        detail="",
    )

    def _fake_fetch_fresh(db_path, *, since=None, provider_id=None, model_id=None, limit=None):
        return [fresh_row]

    monkeypatch.setattr(chain_regen, "fetch_cell_probes", _fake_fetch_fresh)

    result = await gather_and_regenerate(
        Settings(_env_file=None),
        client=None,
        chains_path=target,
        db_path=tmp_path / "db.sqlite",
        enabled=True,
    )
    assert result.written is True
    assert "MODEL_OPUS=nvidia_nim/x/alpha-model,claude_code/opus" in target.read_text(
        encoding="utf-8"
    )


def test_cli_stale_cells_exit_1(tmp_path, monkeypatch) -> None:
    """The CLI catches StaleCellsError, prints the reason, and exits 1."""

    async def _fake_gather_and_regenerate(*args, **kwargs):
        raise chain_regen.StaleCellsError("no cell-probe rows fresher than 12.0h (newest=none)")

    monkeypatch.setattr(chain_regen, "gather_and_regenerate", _fake_gather_and_regenerate)
    chains_path = tmp_path / "chains.env"
    chains_path.write_text(_CHAINS, encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "regenerate-chains",
            "--chains-path",
            str(chains_path),
            "--db-path",
            str(tmp_path / "db.sqlite"),
        ],
    )

    assert result.exit_code == 1
    assert "regenerate-chains: refused" in result.output
    assert "no cell-probe rows fresher than 12.0h" in result.output
