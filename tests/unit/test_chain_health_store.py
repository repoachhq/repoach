"""Unit tests for SP-NIM-HEALTH-HISTORY.

Each test uses a temp-file SQLite DB (``tmp_path``) so nothing touches
the real review database, and the CLI tests patch ``check_tier_heads``
to a scripted sweep so no probe hits the network.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from repoach.review.chain_health import ModelHealth
from repoach.review.chain_health_store import (
    fetch_probes,
    init_nim_health_schema,
    record_probes,
)


def _sweep() -> list[ModelHealth]:
    return [
        ModelHealth("opus", "mistral-medium-3.5", "ok", 0.5, 2, "ok"),
        ModelHealth("sonnet", "mistral-medium-3.5", "empty", 0.4, 0, "http=200"),
        ModelHealth("haiku", "claude_code/haiku", "skipped", None, 0, "non-NIM head"),
    ]


def test_record_and_fetch_round_trip(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    stamp = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)

    written = record_probes(db, _sweep(), recorded_at=stamp)
    rows = fetch_probes(db)

    assert written == 3
    assert len(rows) == 3
    assert {r.tier for r in rows} == {"opus", "sonnet", "haiku"}
    assert all(r.recorded_at == stamp for r in rows)
    assert next(r for r in rows if r.tier == "sonnet").status == "empty"
    assert next(r for r in rows if r.tier == "haiku").latency_s is None


def test_schema_init_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    init_nim_health_schema(db)
    init_nim_health_schema(db)
    assert fetch_probes(db) == []


def test_record_empty_sweep_writes_nothing(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    assert record_probes(db, [], recorded_at=datetime.now(UTC)) == 0
    assert fetch_probes(db) == []


def test_fetch_filters_since(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    old = datetime(2026, 6, 10, 10, 0, tzinfo=UTC)
    new = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)
    record_probes(db, _sweep(), recorded_at=old)
    record_probes(db, _sweep(), recorded_at=new)

    recent = fetch_probes(db, since=new)

    assert len(recent) == 3
    assert all(r.recorded_at == new for r in recent)


def test_fetch_filters_tier(tmp_path: Path) -> None:
    db = tmp_path / "review.db"
    record_probes(db, _sweep(), recorded_at=datetime.now(UTC))
    assert {r.tier for r in fetch_probes(db, tier="opus")} == {"opus"}


def test_cli_persists_probes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "review.db"
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")

    async def _fake_check(*args: Any, **kwargs: Any) -> list[ModelHealth]:
        return _sweep()

    monkeypatch.setattr("repoach.review.chain_health.check_tier_heads", _fake_check)
    result = CliRunner().invoke(app, ["monitor-chains", "--db-path", str(db), "--json"])

    rows = fetch_probes(db)
    assert len(rows) == 3
    assert result.exit_code == 1


def test_cli_no_persist_skips(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db = tmp_path / "review.db"
    monkeypatch.setenv("FEROVA_OPENROUTER_API_KEY", "")

    async def _fake_check(*args: Any, **kwargs: Any) -> list[ModelHealth]:
        return [ModelHealth("sonnet", "m", "ok", 0.4, 2, "ok")]

    monkeypatch.setattr("repoach.review.chain_health.check_tier_heads", _fake_check)
    result = CliRunner().invoke(app, ["monitor-chains", "--db-path", str(db), "--no-persist"])

    assert result.exit_code == 0
    assert fetch_probes(db) == []
