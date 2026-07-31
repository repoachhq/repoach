"""Unit tests for SP-PROXY-ENV-RESIDUE (audit 2026-07-13, low-severity cluster).

Covers the residue items that are pure config/data assertions:

- G1: ``_env_files()`` no longer reads the foreign, out-of-repo
  ``~/.config/free-claude-code/.env`` path; ``chains.env`` stays last.
- G3: ``GlobalRateLimiter`` no longer carries the dead process-wide
  singleton (``_instance`` / ``get_instance``); the live
  ``get_scoped_instance`` path is unaffected.
- G4: the ``effort_map`` module docstring no longer claims the map is
  unwired — it IS seeded at startup and read on the generic transport's
  hot path.
- G5: a probe-seeded breaker trip carries a non-empty ``reason``.

G2 (the ``PTB_TIMEDELTA`` line) and G6 (budget-retry recovery log) are
pure removals / a real-flow log assertion better covered where they live
(``tests/unit/test_proxy_budget_retry_recovers_breaker.py`` for G6); G2 has
no discriminating runtime surface beyond "the line is gone", checked by
inspection during review.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from repoach.health.model_health import ModelHealth
from repoach.health.store import record_probes
from repoach.llm_proxy.config.settings import Settings, _configured_env_files, _env_files
from repoach.llm_proxy.providers import effort_map as effort_map_module
from repoach.llm_proxy.providers.rate_limit import GlobalRateLimiter
from repoach.llm_proxy.routing import get_breaker
from repoach.llm_proxy.routing.probe_seed import seed_breaker_from_probes
from repoach.llm_proxy.routing.refs import ModelRef


def test_env_files_excludes_foreign_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``_env_files()`` never contains the foreign ``free-claude-code`` path,
    and a real ``Settings()`` load (real settings resolution, no
    monkeypatching of Repoach code) resolves the same truthful set."""
    monkeypatch.delenv("FCC_ENV_FILE", raising=False)

    files = _env_files()
    assert not any("free-claude-code" in str(path) for path in files)
    assert files[0] == Path(".env")
    assert files[-1] == Path("chains.env")

    (tmp_path / "chains.env").write_text("MODEL_OPUS=nvidia_nim/x,claude_code/opus\n")
    monkeypatch.chdir(tmp_path)
    settings = Settings()

    resolved = _configured_env_files(settings.model_config)
    assert not any("free-claude-code" in str(path) for path in resolved)


def test_rate_limiter_singleton_removed() -> None:
    """The dead process-wide singleton is gone; scoped limiters still work."""
    assert not hasattr(GlobalRateLimiter, "get_instance")
    assert not hasattr(GlobalRateLimiter, "_instance")

    limiter = GlobalRateLimiter.get_scoped_instance("sp-proxy-env-residue-test")
    assert isinstance(limiter, GlobalRateLimiter)
    assert GlobalRateLimiter.get_scoped_instance("sp-proxy-env-residue-test") is limiter


def test_effort_map_docstring_documents_wiring() -> None:
    """The module docstring no longer claims the effort map is unwired."""
    doc = effort_map_module.__doc__ or ""
    assert "unwired" not in doc
    assert "AppRuntime" in doc


def test_probe_seed_trip_carries_reason(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A probe-seeded breaker trip carries the real, non-empty reason."""
    for key in ("MODEL", "REPOACH_MODEL_HAIKU", "MODEL_HAIKU", "REPOACH_PROXY_DEFAULT_MODEL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("REPOACH_PROXY_DEFAULT_MODEL", "nvidia_nim/default/model")
    monkeypatch.setenv("MODEL_HAIKU", "nvidia_nim/test/haiku-head,claude_code/haiku")
    settings = Settings(_env_file=None)

    db = tmp_path / "p.db"
    record_probes(
        db,
        [ModelHealth("haiku", "test/haiku-head", "error", None, 0, "http=410")],
        recorded_at=datetime(2026, 6, 21, tzinfo=UTC),
    )

    tripped = seed_breaker_from_probes(settings, now=100.0, db_path=db)

    assert tripped == 1
    ref = ModelRef.parse("nvidia_nim/test/haiku-head")
    assert get_breaker()._down_reason[ref] == "provider_410"
