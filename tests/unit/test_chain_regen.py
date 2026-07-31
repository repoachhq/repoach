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
from structlog.testing import capture_logs
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.core.logging import get_logger
from repoach.health.credits import reset_credits_cache
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers import cell_probe, cell_probe_sweep
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


def _reset_module_loggers(monkeypatch) -> None:
    """Rebind fresh, uncached structlog proxies so capture_logs sees this test's events.

    A module-level ``_log`` proxy latches onto whatever processor chain was
    active at its first real use (``cache_logger_on_first_use``); once any
    earlier test in this session has logged through it under a different
    configuration, a later ``capture_logs()`` block never observes its
    events. Rebinding a brand-new proxy here forces its first use to happen
    inside the caller's ``capture_logs()`` context.
    """
    monkeypatch.setattr(chain_regen, "_log", get_logger(chain_regen.__name__))
    monkeypatch.setattr(cell_probe_sweep, "_log", get_logger(cell_probe_sweep.__name__))
    monkeypatch.setattr(cell_probe, "_log", get_logger(cell_probe.__name__))


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


async def test_nominal_via_injected_client_and_ranking(tmp_path) -> None:
    """AC5: a real httpx client over MockTransport plus a pre-built ranking conclude.

    Drives ``gather_and_regenerate`` through its two designed seams — an
    injected ``client`` and a ``ranking=`` keyword — with no monkeypatched
    repoach function. The transport lists a model whose id pattern matches
    the real equivalence table (``deepseek-v4-pro``) and 200s every probe,
    so the in-cycle sweep records a fresh row and the regeneration concludes.
    """
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    db_path = tmp_path / "db.sqlite"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "deepseek-ai/deepseek-v4-pro"}]})
        body = json.loads(request.content or b"{}")
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": f"ok {body.get('model', '')}"}}]},
        )

    settings = Settings(_env_file=None, NVIDIA_NIM_API_KEY="test-token")
    ranking = AaRanking(
        index_version="v4.1",
        models=(
            _cap("Claude Opus 4.7", 53.5),
            _cap("Claude Sonnet 4.6", 47.2),
            _cap("Claude 4.5 Haiku", 29.6),
            _cap("DeepSeek V4 Pro (Max)", 50.0),
        ),
    )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await gather_and_regenerate(
            settings,
            client=client,
            chains_path=target,
            db_path=db_path,
            enabled=True,
            ranking=ranking,
        )

    assert result.written is True
    output = target.read_text(encoding="utf-8")
    assert "MODEL_OPUS=nvidia_nim/deepseek-ai/deepseek-v4-pro,claude_code/opus" in output


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


async def test_bounded_sweep_logs_planned_count(tmp_path, monkeypatch) -> None:
    """AC2: gather_and_regenerate logs regen_sweep_planned with the exact bounded cell count.

    The three chain-ref cells parsed from ``_CHAINS`` (provider ``old``) plus the
    one ranking-matched candidate cell (``nvidia_nim/x/alpha-model``) sum to a
    planned set of 4 cells, with no per-provider cap or credits skip in play under
    the default Settings.
    """
    _patch_gatherers(monkeypatch)
    _reset_module_loggers(monkeypatch)
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")

    with capture_logs() as logs:
        await gather_and_regenerate(
            Settings(_env_file=None),
            client=None,
            chains_path=target,
            db_path=tmp_path / "db.sqlite",
            enabled=False,
        )

    planned = [entry for entry in logs if entry["event"] == "regen_sweep_planned"]
    assert len(planned) == 1
    assert planned[0]["cells"] == 4
    assert planned[0]["skipped_paid"] == 0


async def test_429_logs_rate_limited_once(monkeypatch) -> None:
    """AC3: a twice-429 cell logs cell_probe_rate_limited exactly once; the peer persists."""
    _reset_module_loggers(monkeypatch)
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
        with capture_logs() as logs:
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
    rate_limited_events = [entry for entry in logs if entry["event"] == "cell_probe_rate_limited"]
    assert len(rate_limited_events) == 1
    assert rate_limited_events[0]["provider"] == "nvidia_nim"
    assert rate_limited_events[0]["model"] == "a"


async def test_sweep_failure_folds_into_stale_refusal(tmp_path, monkeypatch) -> None:
    """A sweep that raises mid-cycle yields StaleCellsError, not the original exception.

    The ``chain_regen_sweep_failed`` event is captured and the freshness guard
    raises ``StaleCellsError`` rather than letting the raw exception escape.
    """
    _patch_gatherers(monkeypatch)
    _reset_module_loggers(monkeypatch)

    async def _failing_sweep(*args, **kwargs):
        raise RuntimeError("probe transport down")

    monkeypatch.setattr(chain_regen, "_sweep_and_persist_bounded_cells", _failing_sweep)

    stale_row = _row("nvidia_nim", "x/alpha-model", 1.0, day=1)

    def _fake_fetch_stale(db_path, *, since=None, provider_id=None, model_id=None, limit=None):
        return [stale_row]

    monkeypatch.setattr(chain_regen, "fetch_cell_probes", _fake_fetch_stale)

    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    settings = Settings(_env_file=None, REGEN_MAX_CELL_AGE_H=1.0)

    with capture_logs() as logs, pytest.raises(chain_regen.StaleCellsError):
        await gather_and_regenerate(
            settings,
            client=None,
            chains_path=target,
            db_path=tmp_path / "db.sqlite",
            enabled=True,
        )

    sweep_failed = [e for e in logs if e["event"] == "chain_regen_sweep_failed"]
    assert len(sweep_failed) == 1
    assert "probe transport down" in sweep_failed[0]["error"]


async def test_credits_skip_transport_silent_and_logged(tmp_path) -> None:
    """AC4: a below-floor credits snapshot drops open_router cells before any probe
    reaches the transport, and the drop count is logged on regen_sweep_planned.
    """
    reset_credits_cache()
    calls = {"total": 0, "credits": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["total"] += 1
        if request.url.path.endswith("/credits"):
            calls["credits"] += 1
            return httpx.Response(200, json={"data": {"total_credits": 1.0, "total_usage": 0.5}})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    settings = Settings(_env_file=None, OPENROUTER_API_KEY="test-token", CREDITS_FLOOR_USD=1.0)
    current_content = "MODEL_OPUS=open_router/free-model\n"
    matrix = ProviderModelMatrix(cells=(), listings=())
    monkeypatch = pytest.MonkeyPatch()
    _reset_module_loggers(monkeypatch)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with capture_logs() as logs:
            await chain_regen._sweep_and_persist_bounded_cells(
                settings,
                client,
                matrix,
                _equivalences(),
                _ranking(),
                current_content,
                tmp_path / "db.sqlite",
            )

    assert calls["total"] == 1
    assert calls["credits"] == 1
    planned = [entry for entry in logs if entry["event"] == "regen_sweep_planned"]
    assert len(planned) == 1
    assert planned[0]["cells"] == 0
    assert planned[0]["skipped_paid"] == 1
