"""End-to-end freshness refusal for the model-first regeneration (SP-REGEN-FRESH-CELLS).

Drives ``gather_and_regenerate`` directly with a fake HTTP transport and a
tmp-path SQLite — the Interface's two designed seams (an injected ``client``
and a pre-built ``ranking=``), no monkeypatched repoach functions. The
pre-seeded probe row is older than ``max_cell_age_h`` and the fake transport
429s every chat-completion probe of the in-cycle sweep (cap > 0, so the sweep
is genuinely active), so the sweep contributes zero fresh rows — forcing the
G5 refusal: ``StaleCellsError`` propagates, ``chain_regen_stale_cells`` is
logged, and ``chains.env`` is left untouched.
"""

from __future__ import annotations

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
        "MODEL_OPUS=nvidia_nim/x/alpha-model",
        "MODEL_SONNET=nvidia_nim/x/alpha-model",
        "MODEL_HAIKU=nvidia_nim/x/alpha-model",
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


def _rate_limited_handler(request: httpx.Request) -> httpx.Response:
    """A fake transport: lists one NIM model, 429s every chat-completion probe."""
    if request.url.path.endswith("/models"):
        return httpx.Response(200, json={"data": [{"id": "x/alpha-model"}]})
    return httpx.Response(429, json={"error": "rate limited"})


async def test_stale_after_real_sweep_refuses(tmp_path: Path) -> None:
    """AC1: a real cap>0 sweep whose probes all 429 yields no fresh rows; refuses loudly.

    The pre-seeded row is 48h old, well outside the 1h freshness window. The
    in-cycle sweep is genuinely active (cap=3, not the 0 escape hatch) and
    probes the one discovered NIM cell, but every probe comes back 429 — so
    :func:`chain_regen._probe_bounded_cells` retries once, still 429, drops
    it, and persists nothing this cycle. The since-windowed read therefore
    sees only the stale pre-seeded row and refuses.
    """
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
        REGEN_SWEEP_PER_PROVIDER_CAP=3,
        REGEN_SWEEP_PACING_S=0.0,
        REGEN_SWEEP_RETRY_BACKOFF_S=0.0,
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(_rate_limited_handler)) as client:
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
