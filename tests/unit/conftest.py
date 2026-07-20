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

from repoach.llm_proxy.routing import reset_breaker


@pytest.fixture(autouse=True)
def _reset_health_breaker() -> None:
    """Clear the process-level failover breaker before each test.

    The breaker (SP-PROXY-HEALTH-BREAKER) is a singleton; a trip left by
    one test would leak into the next and reshape its resolved chain.
    """
    reset_breaker()


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
