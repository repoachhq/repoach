"""Shared test-session hermeticity defaults for both suites.

Sits above ``tests/unit/`` and ``tests/integration/`` so pytest applies
its autouse fixtures to both without duplication.
"""

from __future__ import annotations

import pytest
import structlog

from repoach.review.base_ref import ResolvedBase


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


@pytest.fixture(autouse=True)
def _no_legacy_anchors(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ignore a repoach checkout's legacy ``.env`` and ``data/repoach.db``.

    SP-UNIVERSAL-INSTALL keeps honouring both in repoach's own checkout
    (with a deprecation notice). The suite runs from such a checkout, so
    without this every ``Settings()`` would read the developer's real
    ``.env`` and point at the live ledger. Tests of the legacy path
    restore the real functions locally.
    """
    monkeypatch.setattr("repoach.core.paths.legacy_env_file", lambda _root: None)
    monkeypatch.setattr("repoach.core.paths.legacy_db_path", lambda _root: None)


FAKE_SHA = "0" * 40
"""The tip and merge base ``base_resolves`` reports: no commit of any clone, never a real value."""

BASE_RESOLVERS = (
    "repoach.review.orchestrator.require_forge_pr_base",
    "repoach.review.auto_merge.require_forge_pr_base",
    "repoach.review.coder_findings.require_forge_pr_base",
    "repoach.review.release_gate.require_merge_base",
)
"""Every call site of the base resolution (Yoob B-221) that ``base_resolves`` replaces."""


@pytest.fixture
def base_resolves(monkeypatch: pytest.MonkeyPatch) -> None:
    """Let the base resolve at every call site of :data:`BASE_RESOLVERS`.

    For a module that drives the review, the gate, the Coder loop or the
    release gate on a fake clone (no git repository, or a scripted forge
    ``git``), declared with ``pytestmark = pytest.mark.usefixtures("base_resolves")``.
    The resolution itself is tested in ``tests/unit/test_base_ref_must_resolve.py``
    and ``tests/integration/test_base_ref_must_resolve_yoob_324.py``.
    """
    resolved = ResolvedBase(ref="develop", tip=FAKE_SHA, merge_base=FAKE_SHA)
    for target in BASE_RESOLVERS:
        monkeypatch.setattr(target, lambda *_a, **_k: resolved)
