"""Tests for the breaker probe-seed (SP-PROXY-BREAKER-PROBE-SEED, D2b).

A degraded latest probe pre-trips its tier's chain head with a reason-aware
TTL; an EOL 410 gets the terminal window, a transient the short one; a
healthy or absent probe seeds nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from ferova.health.model_health import ModelHealth
from ferova.health.store import record_probes
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.routing import get_breaker
from ferova.llm_proxy.routing.probe_seed import (
    parse_tier,
    reason_from_detail,
    seed_breaker_from_probes,
)
from ferova.llm_proxy.routing.refs import ModelRef
from ferova.llm_proxy.routing.tier import Tier


def _settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    for key in ("MODEL", "FEROVA_MODEL_HAIKU", "MODEL_HAIKU", "FEROVA_PROXY_DEFAULT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FEROVA_PROXY_DEFAULT_MODEL", "nvidia_nim/default/model")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/test/haiku-head,claude_code/haiku")
    return Settings(_env_file=None)


def _haiku_head() -> ModelRef:
    return ModelRef.parse("nvidia_nim/test/haiku-head")


def _write(db: Path, *probes: ModelHealth) -> None:
    record_probes(db, list(probes), recorded_at=datetime(2026, 6, 21, tzinfo=UTC))


def test_parse_tier_and_reason() -> None:
    assert parse_tier("haiku") is Tier.HAIKU
    assert parse_tier("coder") is None
    assert parse_tier("nonsense") is None
    assert reason_from_detail("http=410") == "provider_410"
    assert reason_from_detail("ReadTimeout") == "probe_degraded"


def test_410_probe_trips_head_for_terminal_ttl(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch)
    db = tmp_path / "p.db"
    _write(db, ModelHealth("haiku", "test/haiku-head", "error", None, 0, "http=410"))

    tripped = seed_breaker_from_probes(settings, now=100.0, db_path=db)

    assert tripped == 1
    breaker = get_breaker()
    assert breaker.is_down(_haiku_head(), now=100.0 + settings.breaker_ttl_s + 5.0)
    assert not breaker.is_down(_haiku_head(), now=100.0 + settings.breaker_ttl_terminal_s + 5.0)


def test_transient_probe_trips_only_short_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    settings = _settings(monkeypatch)
    db = tmp_path / "p.db"
    _write(db, ModelHealth("haiku", "test/haiku-head", "error", None, 0, "ReadTimeout"))

    seed_breaker_from_probes(settings, now=100.0, db_path=db)

    assert not get_breaker().is_down(_haiku_head(), now=100.0 + settings.breaker_ttl_s + 5.0)


def test_healthy_probe_seeds_nothing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(monkeypatch)
    db = tmp_path / "p.db"
    _write(db, ModelHealth("haiku", "test/haiku-head", "ok", 2.0, 2, "ok"))

    assert seed_breaker_from_probes(settings, now=100.0, db_path=db) == 0
    assert not get_breaker().is_down(_haiku_head(), now=101.0)


def test_latest_probe_wins(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(monkeypatch)
    db = tmp_path / "p.db"
    record_probes(
        db,
        [ModelHealth("haiku", "test/haiku-head", "error", None, 0, "http=410")],
        recorded_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    record_probes(
        db,
        [ModelHealth("haiku", "test/haiku-head", "ok", 2.0, 2, "ok")],
        recorded_at=datetime(2026, 6, 21, tzinfo=UTC),
    )

    assert seed_breaker_from_probes(settings, now=100.0, db_path=db) == 0


def test_missing_db_returns_zero(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    settings = _settings(monkeypatch)
    assert seed_breaker_from_probes(settings, now=100.0, db_path=tmp_path / "nope.db") == 0
