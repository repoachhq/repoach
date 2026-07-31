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
