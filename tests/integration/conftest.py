"""Integration-suite hermeticity defaults.

`SP-PROXY-STATE-PERSIST` gives the failover breaker a write-through to
``settings.breaker_probe_seed_db`` — the SAME shared SQLite file
(``data/repoach.db`` by default) the deployed proxy's probe-seed and
effort-map steps already read from. Before this spec only reads happened
against that default path; now a live dispatch failure WRITES a durable
row, and every ``AppRuntime.startup()`` call (from a fresh ``create_app()``
+ ``TestClient``) rehydrates and prunes it. Any integration test that
builds a real app without explicitly overriding the setting would
otherwise mutate the real, out-of-repo database file the running proxy
depends on — a hermeticity violation this autouse fixture closes for the
whole suite.

``repoach.llm_proxy.api.app`` calls ``get_settings()`` at MODULE IMPORT
time (its ``_settings = get_settings()`` line), which happens once at
collection — before any per-test fixture, including this one, has run —
so ``get_settings``'s ``lru_cache`` is already primed with the real
environment's default path by the time the first test body executes.
Clearing the cache here (after the env override is set, and again on
teardown) forces every test's first ``get_settings()`` call to rebuild
from the CURRENT environment, so this fixture's override actually takes
effect even for a test that never calls ``get_settings.cache_clear()``
itself. A test that needs a specific path still wins: its own
``monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", ...)`` call runs
later on the same ``monkeypatch`` fixture instance and overrides this
default.
"""

from __future__ import annotations

import pytest

from repoach.llm_proxy.config.settings import get_settings


@pytest.fixture(autouse=True)
def _hermetic_breaker_state_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
):
    """Point ``breaker_probe_seed_db`` at an isolated tmp file by default."""
    db = tmp_path_factory.mktemp("breaker_state") / "repoach.db"
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _hermetic_llm_proxy_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``REPOACH_LLM_PROXY_BASE_URL`` so ``core.config.Settings``
    always resolves without touching the real deployed ``.env``.

    SP-CONFIG-ENV-ANCHOR: ``Settings`` now fails loud at construction
    when neither an anchored env file nor the process environment
    resolves ``llm_proxy_base_url`` (the retired ``:8082`` fallback is
    gone, on purpose). CI checks out no ``.env`` (it is gitignored) and
    the committed ``chains.env`` never carried this key, so without a
    default here the first test in a worker process to reach a real
    ``core.config.Settings()``/``get_settings()`` would raise. A test
    exercising the unresolved-raise path itself deletes the var later
    on the same ``monkeypatch`` fixture instance, which wins.
    """
    monkeypatch.setenv("REPOACH_LLM_PROXY_BASE_URL", "http://llm-proxy.test.invalid:9999")
