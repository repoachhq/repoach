"""Tests for SP-CHAINPILOT-LOOP (Phase 3e).

Covers the pure healthy-cell selection, the network-free decide→plan→apply core
(plan_and_apply, with an injected matrix + a probe DB), and an offline smoke of
the full async cycle with a fake HTTP client.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.benchmark_equivalences import load_equivalence_table
from repoach.llm_proxy.providers.benchmark_prior import load_benchmark_ranking
from repoach.llm_proxy.providers.cell_probe import CellHealth
from repoach.llm_proxy.providers.cell_probe_store import CellProbeRow, record_cell_probes
from repoach.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from repoach.review.chain_loop import (
    CycleReport,
    plan_and_apply,
    run_autopilot_cycle,
    select_healthy_cells,
)

_NOW = datetime(2026, 6, 27, 12, 0, tzinfo=UTC)

_CHAINS = """# Chain config.
MODEL_OPUS=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/opus
MODEL_SONNET=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/sonnet
MODEL_HAIKU=nvidia_nim/mistralai/mistral-small-4-119b-2603,claude_code/haiku
"""


def _probe_row(provider: str, model: str, status: str) -> CellProbeRow:
    return CellProbeRow(
        recorded_at=_NOW,
        provider_id=provider,
        model_id=model,
        status=status,
        latency_s=1.0,
        content_chars=10,
        reasoning_chars=0,
        detail="",
    )


def _health(provider: str, model: str, status: str = "ok") -> CellHealth:
    return CellHealth(
        provider_id=provider,
        model_id=model,
        status=status,
        latency_s=1.0,
        content_chars=10,
        reasoning_chars=0,
        detail="",
    )


def _matrix(cells: list[ModelCell]) -> ProviderModelMatrix:
    return ProviderModelMatrix(cells=tuple(cells), listings=())


def _slot(content: str, key: str) -> str:
    for line in content.split("\n"):
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    raise AssertionError(f"{key} not found")


def test_select_healthy_cells() -> None:
    probes = [
        _probe_row("nvidia_nim", "z-ai/glm-5.2", "ok"),
        _probe_row("nvidia_nim", "z-ai/glm-5.2", "ok"),
        _probe_row("nvidia_nim", "z-ai/glm-5.2", "ok"),
        _probe_row("nvidia_nim", "dead/model", "error"),
        _probe_row("nvidia_nim", "dead/model", "error"),
        _probe_row("nvidia_nim", "dead/model", "error"),
        _probe_row("nvidia_nim", "thin/model", "ok"),
    ]
    healthy = select_healthy_cells(probes)
    assert ("nvidia_nim", "z-ai/glm-5.2") in healthy
    assert ("nvidia_nim", "dead/model") not in healthy
    assert ("nvidia_nim", "thin/model") not in healthy


def _seed_glm_healthy(db: Path) -> None:
    record_cell_probes(
        db,
        [_health("nvidia_nim", "z-ai/glm-5.2") for _ in range(3)],
        recorded_at=_NOW,
    )


def test_plan_and_apply_cold_starts_a_healthy_absent_model(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_CHAINS, encoding="utf-8")
    _seed_glm_healthy(db)

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2")]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        enabled=True,
    )

    assert isinstance(report, CycleReport)
    assert report.cold_starts >= 1
    assert report.written is True
    assert "z-ai/glm-5.2" in chains.read_text()


def test_plan_and_apply_shadow_leaves_chains_untouched(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_CHAINS, encoding="utf-8")
    _seed_glm_healthy(db)

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([ModelCell(provider_id="nvidia_nim", model_id="z-ai/glm-5.2")]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        enabled=False,
    )

    assert report.written is False
    assert chains.read_text() == _CHAINS


def test_plan_and_apply_empty_matrix_is_noop(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_CHAINS, encoding="utf-8")

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        enabled=True,
    )

    assert report.cold_starts == 0
    assert report.written is False
    assert chains.read_text() == _CHAINS


_STALE_CHAINS = (
    "MODEL_OPUS=nvidia_nim/dead/model,claude_code/opus\n"
    "MODEL_SONNET=nvidia_nim/mistralai/mistral-medium-3.5-128b,claude_code/sonnet\n"
    "MODEL_HAIKU=nvidia_nim/mistralai/mistral-small-4-119b-2603,claude_code/haiku\n"
)


def _record_errors(db: Path, when: datetime) -> None:
    record_cell_probes(
        db,
        [_health("nvidia_nim", "dead/model", "error") for _ in range(3)],
        recorded_at=when,
    )


def test_plan_and_apply_windows_out_stale_faults(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_STALE_CHAINS, encoding="utf-8")
    _record_errors(db, _NOW - timedelta(hours=48))

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        lookback=timedelta(hours=24),
        enabled=True,
    )

    assert report.written is False
    assert "dead/model" in _slot(chains.read_text(), "MODEL_OPUS")


def test_plan_and_apply_evicts_on_recent_faults(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_STALE_CHAINS, encoding="utf-8")
    _record_errors(db, _NOW)

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        lookback=timedelta(hours=24),
        enabled=True,
    )

    assert report.written is True
    assert "dead/model" not in _slot(chains.read_text(), "MODEL_OPUS")


def test_max_mutations_cap_threads_through(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_STALE_CHAINS, encoding="utf-8")
    _record_errors(db, _NOW)

    report = plan_and_apply(
        db,
        chains,
        matrix=_matrix([]),
        ranking=load_benchmark_ranking(),
        equivalences=load_equivalence_table(),
        recorded_at=_NOW,
        lookback=timedelta(hours=24),
        enabled=True,
        max_mutations=0,
    )

    assert report.written is False
    assert "dead/model" in _slot(chains.read_text(), "MODEL_OPUS")


class _Resp:
    status_code = 200

    def json(self) -> dict:
        return {"data": []}

    def raise_for_status(self) -> None:
        return None

    @property
    def text(self) -> str:
        return "{}"


class _FakeClient:
    async def get(self, *args: object, **kwargs: object) -> _Resp:
        return _Resp()

    async def post(self, *args: object, **kwargs: object) -> _Resp:
        return _Resp()


def test_run_autopilot_cycle_smoke_offline(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_CHAINS, encoding="utf-8")

    report = asyncio.run(
        run_autopilot_cycle(
            settings=Settings(),
            client=_FakeClient(),
            db_path=db,
            chains_path=chains,
            ranking=load_benchmark_ranking(),
            equivalences=load_equivalence_table(),
            recorded_at=_NOW,
            enabled=False,
        )
    )

    assert isinstance(report, CycleReport)
    assert report.written is False
    assert chains.read_text() == _CHAINS


def test_freshness_gate_blocks_write_when_nothing_swept(tmp_path: Path) -> None:
    db = tmp_path / "data.db"
    chains = tmp_path / "chains.env"
    chains.write_text(_STALE_CHAINS, encoding="utf-8")
    _record_errors(db, _NOW)

    report = asyncio.run(
        run_autopilot_cycle(
            settings=Settings(),
            client=_FakeClient(),
            db_path=db,
            chains_path=chains,
            ranking=load_benchmark_ranking(),
            equivalences=load_equivalence_table(),
            recorded_at=_NOW,
            enabled=True,
        )
    )

    assert report.written is False
    assert chains.read_text() == _STALE_CHAINS
