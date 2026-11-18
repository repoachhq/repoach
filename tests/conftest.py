"""Shared test-session hermeticity defaults for both suites.

Sits above ``tests/unit/`` and ``tests/integration/`` so pytest applies
its autouse fixtures to both without duplication.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import structlog

_SESSION_XDG_CONFIG_HOME = os.environ.get("XDG_CONFIG_HOME")
"""``XDG_CONFIG_HOME`` as the process found it, read when this conftest loads.

That is before :func:`_hermetic_xdg_dirs` points it at a scratch
directory for every test, so :func:`real_user_config_dir` can name the
directory the developer (or the CI runner) actually configured.
"""


@pytest.fixture(scope="session")
def real_user_config_dir() -> Path:
    """The real ``user_config_dir()``, as it was before the suite made the XDG dirs hermetic.

    Only a test that must read the maintainer's real, untracked
    configuration asks for it: the deny-list test of
    SP-RECORDER-REDACTS-BY-CLASS reads ``personal-denylist`` there
    (SP-NO-PERSONAL-DATA-GATE D3, the file merged with
    ``REPOACH_PERSONAL_DENYLIST``). Every other test keeps the scratch
    directory. The XDG rule of ``repoach.core.paths.user_config_dir`` (only
    an absolute value counts) is restated rather than imported, so this
    conftest imports no package module; the test that asks for the
    fixture holds the two equal.
    """
    raw = _SESSION_XDG_CONFIG_HOME or ""
    base = Path(raw) if raw and Path(raw).is_absolute() else Path.home() / ".config"
    return base / "repoach"


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


@pytest.fixture(autouse=True)
def _no_transport_retry_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Close the transport retry window of every model call the suite makes.

    SP-REVIEW-SURVIVES-TRANSPORT-FAILURES gives each call a 300 s window
    in which a transport error is retried with real sleeps. The suite's
    fake providers raise transport errors on purpose to pin what happens
    after the retries; with the window open, each would wait for minutes.
    The seam is the loop's reading of the window, not the environment, so
    ``repoach config show`` still reports what the files say; the tests of
    the window itself restore
    :func:`~repoach.agent_engine.transport_window.configured_transport_window_s`.
    """
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.configured_transport_window_s", lambda _route: 0.0
    )


@pytest.fixture(autouse=True)
def _no_harvest_forge(monkeypatch: pytest.MonkeyPatch) -> None:
    """Refuse every ``gh`` read of the affected-jobs harvest.

    ``repoach release gate`` harvests the last merged pull requests for
    its affected-jobs report line (SP-AFFECTED-JOBS-MISSES-ARE-TEST-CASES
    D5). Un-patched, every test of the gate would make hundreds of live
    ``gh`` calls on an authenticated box and none on CI. The seam is
    :func:`repoach.affected.gh.run_gh`, read at call time; the
    harvest tests pass their own runner, and the test of ``run_gh``
    itself holds the function it imported before this patch.
    """

    def _refuse(*args: str) -> str:
        from repoach.affected.gh import HarvestError

        raise HarvestError(["gh", *args], "the test suite reads no forge")

    monkeypatch.setattr("repoach.affected.gh.run_gh", _refuse)


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

    The import of :mod:`repoach.review.base_ref` stays inside the fixture:
    this conftest loads in every pytest process, including the subprocess
    runs of ``test_slow_settings_isolation_end_to_end.py`` under a 60 s
    timeout, and must not pull the review package into each of them.
    """
    from repoach.review.base_ref import ResolvedBase

    resolved = ResolvedBase(ref="develop", tip=FAKE_SHA, merge_base=FAKE_SHA)
    for target in BASE_RESOLVERS:
        monkeypatch.setattr(target, lambda *_a, **_k: resolved)
