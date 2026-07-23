"""Unit-suite hermeticity defaults (default-network sentinel).

The orchestrator recalls review-scoped agentmemory lessons on every
``review_pr`` run (SP-REVIEW-MEMORY). On a developer box with the
service up, un-patched tests would silently perform live recalls —
test pollution that CI cannot reproduce. This autouse fixture stubs
the recall to ``[]`` for every unit test; tests that exercise the
recall itself override the patch locally (their ``monkeypatch.setattr``
runs after this one and wins).
"""

from __future__ import annotations

import pytest

from repoach.llm_proxy.config.settings import get_settings
from repoach.llm_proxy.routing import reset_breaker


@pytest.fixture(autouse=True)
def _reset_health_breaker() -> None:
    """Clear the process-level failover breaker before each test.

    The breaker (SP-PROXY-HEALTH-BREAKER) is a singleton; a trip left by
    one test would leak into the next and reshape its resolved chain.
    """
    reset_breaker()


@pytest.fixture(autouse=True)
def _hermetic_breaker_state_db(
    monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory
):
    """Point ``breaker_probe_seed_db`` at an isolated tmp file by default.

    `SP-PROXY-STATE-PERSIST` gives the failover breaker a write-through to
    ``settings.breaker_probe_seed_db`` — the SAME shared SQLite file
    (``data/repoach.db`` by default) the deployed proxy uses. A unit test
    that builds a real app (e.g. ``test_health_breaker.py``'s
    ``create_app()`` calls) without overriding this setting would
    otherwise mutate that real, out-of-repo file. Clearing
    ``get_settings``'s cache is required too: ``repoach.llm_proxy.api.app``
    calls ``get_settings()`` at MODULE IMPORT time, priming the
    ``lru_cache`` with the real environment's default before any per-test
    fixture — including this one — has run.
    """
    db = tmp_path_factory.mktemp("breaker_state") / "repoach.db"
    monkeypatch.setenv("REPOACH_BREAKER_PROBE_SEED_DB", str(db))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _no_live_review_memory_recall(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "repoach.review.orchestrator.recall_review_lessons",
        lambda _query: [],
    )


@pytest.fixture(autouse=True)
def _no_live_refuter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the orchestrator's refuter call (SP-REFUTER), which would
    otherwise build an OPUS AgentLoop on design/security findings and
    reach the live LLM. Refuter-internal tests inject a fake judge
    directly; orchestrator tests that need the call override this.
    """
    monkeypatch.setattr(
        "repoach.review.orchestrator.judge_findings_for_pr",
        lambda *_args, **_kwargs: {"verified": 0, "refuted": 0, "deferred": 0},
    )
