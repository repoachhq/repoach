"""Shared test-session hermeticity defaults for both suites.

Sits above ``tests/unit/`` and ``tests/integration/`` so pytest applies
its autouse fixtures to both without duplication.
"""

from __future__ import annotations

import pytest
import structlog


@pytest.fixture(autouse=True)
def _disable_structlog_logger_cache_for_tests() -> None:
    """Reset ``cache_logger_on_first_use`` to ``False`` before every test.

    ``configure_logging`` (``src/repoach/core/logging.py``, exercised by
    any ``CliRunner``-driven test through ``cli/main.py``) unconditionally
    sets ``cache_logger_on_first_use=True`` and installs a fresh
    processors list. A bound logger whose first-ever real call lands
    while that flag is ``True`` permanently freezes a reference to
    whichever processors list is current at that instant; a later,
    unrelated ``configure_logging`` call replaces that list with a new
    object, so the frozen logger's output never reaches a subsequent
    ``structlog.testing.capture_logs`` — invisible, in a way that depends
    on serial run order. Because the flag is re-read on every unfrozen call,
    resetting it before EACH test (not once per session) is required: a
    fixture that ran only at session start would not survive any later
    CLI-invoking test re-enabling the flag for everything after it.
    """
    structlog.configure(cache_logger_on_first_use=False)


@pytest.fixture(autouse=True)
def _hermetic_xdg_dirs(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """Point ``XDG_CONFIG_HOME`` and ``XDG_DATA_HOME`` at session scratch dirs.

    SP-UNIVERSAL-INSTALL: ``core.config.Settings`` reads
    ``$XDG_CONFIG_HOME/repoach/env`` and defaults the ledger under
    ``$XDG_DATA_HOME/repoach/``. Without this, a developer's real
    ``~/.config/repoach/env`` would leak into the suite and every
    ``get_settings()`` would create directories under the real
    ``~/.local/share/repoach``. One directory per session (not per test)
    because ``get_settings()`` caches its first instance process-wide.
    """
    base = tmp_path_factory.getbasetemp()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(base / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(base / "xdg_data"))
