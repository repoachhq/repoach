"""Unit suite for the model-first live gather + regenerate entrypoint (SP-MFC-REGEN).

The gather functions are monkeypatched in the module namespace, so no test
performs live network or DB I/O. Covers the latency reduction, the compose, the
apply gate, and the CLI command registration.
"""

from __future__ import annotations

from datetime import UTC, datetime

from typer.testing import CliRunner

from ferova.cli.main import app
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.aa_ingest import (
    AaRanking,
    ModelCapability,
    normalize_model_name,
)
from ferova.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from ferova.llm_proxy.providers.cell_probe_store import CellProbeRow
from ferova.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from ferova.llm_proxy.routing import chain_regen
from ferova.llm_proxy.routing.chain_regen import (
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


def _patch_gatherers(monkeypatch) -> None:
    """Stub every live gather function in the chain_regen namespace."""

    async def _fake_sweep(settings, client):
        return _matrix()

    monkeypatch.setattr(chain_regen, "fetch_aa_ranking", lambda settings: _ranking())
    monkeypatch.setattr(chain_regen, "sweep_model_matrix", _fake_sweep)
    monkeypatch.setattr(chain_regen, "load_equivalence_table", lambda: _equivalences())
    monkeypatch.setattr(chain_regen, "fetch_cell_probes", lambda db_path: [])


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
