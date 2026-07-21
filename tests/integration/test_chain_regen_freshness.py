"""End-to-end freshness refusal for the model-first regeneration (SP-REGEN-FRESH-CELLS).

Drives ``gather_and_regenerate`` directly with a fake HTTP transport and a
tmp-path SQLite — the Interface's two designed seams (an injected ``client``
and a pre-built ``ranking=``), no monkeypatched repoach functions. The
per-provider sweep cap is set to ``0`` (the documented operational escape
hatch), so the in-cycle sweep persists nothing this cycle and the
since-windowed read sees only the pre-seeded stale row, forcing the G5
refusal: ``StaleCellsError`` propagates and ``chains.env`` is left untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.aa_ingest import AaRanking, ModelCapability, normalize_model_name
from repoach.llm_proxy.providers.cell_probe_store import record_cell_probes
from repoach.llm_proxy.providers.cell_probe_sweep import CellHealth
from repoach.llm_proxy.routing.chain_regen import StaleCellsError, gather_and_regenerate

structlog.configure(cache_logger_on_first_use=False)

_CHAINS = "\n".join(
    [
        "# header",
        "MODEL_OPUS=old/opus",
        "MODEL_SONNET=old/sonnet",
        "MODEL_HAIKU=old/haiku",
        "",
    ]
)


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
    """A minimal pre-built ranking, injected via the Interface's ranking= seam."""
    return AaRanking(
        index_version="v4.1",
        models=(
            _cap("Claude Opus 4.7", 53.5),
            _cap("Claude Sonnet 4.6", 47.2),
            _cap("Claude 4.5 Haiku", 29.6),
        ),
    )


def _handler(request: httpx.Request) -> httpx.Response:
    """A fake transport: lists one NIM model, and would 200 any probe."""
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "x/alpha-model"}]})
    body = json.loads(request.content or b"{}")
    return httpx.Response(
        200, json={"choices": [{"message": {"content": f"ok {body.get('model', '')}"}}]}
    )


async def test_end_to_end_freshness_refusal(tmp_path: Path) -> None:
    """A stale-only probe store refuses loudly and writes no chains output."""
    chains_path = tmp_path / "chains.env"
    chains_path.write_text(_CHAINS, encoding="utf-8")
    db_path = tmp_path / "cell_health.sqlite"

    stale_health = CellHealth("nvidia_nim", "x/alpha-model", "ok", 1.0, 10, 0, "")
    record_cell_probes(
        db_path,
        [stale_health],
        recorded_at=datetime.now(UTC) - timedelta(hours=48),
    )

    settings = Settings(
        _env_file=None,
        NVIDIA_NIM_API_KEY="test-token",
        REGEN_MAX_CELL_AGE_H=1.0,
        REGEN_SWEEP_PER_PROVIDER_CAP=0,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with pytest.raises(StaleCellsError):
            await gather_and_regenerate(
                settings,
                client=client,
                chains_path=chains_path,
                db_path=db_path,
                enabled=True,
                ranking=_ranking(),
            )

    assert chains_path.read_text(encoding="utf-8") == _CHAINS


async def test_stale_cells_event_logged(tmp_path: Path) -> None:
    """AC1: the refusal path logs chain_regen_stale_cells before raising StaleCellsError."""
    chains_path = tmp_path / "chains.env"
    chains_path.write_text(_CHAINS, encoding="utf-8")
    db_path = tmp_path / "cell_health.sqlite"

    stale_health = CellHealth("nvidia_nim", "x/alpha-model", "ok", 1.0, 10, 0, "")
    record_cell_probes(
        db_path,
        [stale_health],
        recorded_at=datetime.now(UTC) - timedelta(hours=48),
    )

    settings = Settings(
        _env_file=None,
        NVIDIA_NIM_API_KEY="test-token",
        REGEN_MAX_CELL_AGE_H=1.0,
        REGEN_SWEEP_PER_PROVIDER_CAP=0,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_handler)) as client:
        with capture_logs() as logs:
            with pytest.raises(StaleCellsError):
                await gather_and_regenerate(
                    settings,
                    client=client,
                    chains_path=chains_path,
                    db_path=db_path,
                    enabled=True,
                    ranking=_ranking(),
                )

    assert chains_path.read_text(encoding="utf-8") == _CHAINS
    stale_events = [entry for entry in logs if entry["event"] == "chain_regen_stale_cells"]
    assert len(stale_events) == 1
    assert stale_events[0]["max_age_h"] == 1.0
