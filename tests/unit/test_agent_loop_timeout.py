"""Tests for SP-NIM-RES-V3 — long-output per-call timeout.

The tests assert the constants + propagation, NOT the underlying
``httpx`` client behaviour: the goal is to lock in that the default
timeout is long enough for a 32K-token Developer JSON to land
mid-stream without an APIConnectionError.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ferova.agent_engine.agent_loop import LONG_OUTPUT_TIMEOUT_S, AgentLoop
from ferova.review.reviewer import Developer


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep AgentLoop constructible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit ``freecc`` token
    fallback, so a tokenless environment (CI) now refuses to build the
    loop — these tests target timeouts, not auth.
    """
    monkeypatch.setattr(
        "ferova.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def test_long_output_timeout_covers_full_token_budget() -> None:
    """Cap must cover a full 32K-token generation at measured speed.

    Observed 2026-05-06 (PR #96 dogfood): qwen3-coder-480b legitimately
    runs ~9 min wall-clock on a 60KB diff via the proxy.  Observed
    2026-06-07 (SP-PLAN-FORM dispatch post-mortem): ~21 tokens/s means
    a 32K-token Developer response needs ~25 min; a 720 s cap made the
    client abandon while the server kept generating, and the stacked
    zombie generations starved the proxy.  Anything under 1800 s
    re-opens that failure mode.
    """
    assert LONG_OUTPUT_TIMEOUT_S >= 1800


def test_default_per_call_timeout_is_long_enough_for_developer() -> None:
    """An ``AgentLoop()`` constructed without args must default to the
    long-output timeout — that's what every Developer / Coder dispatch
    relies on.
    """
    loop = AgentLoop()
    assert loop._per_call_timeout_s == LONG_OUTPUT_TIMEOUT_S
    assert loop._per_call_timeout_s >= 300


def test_developer_passes_long_output_timeout_to_loop() -> None:
    """``Developer()`` (no args) must wire the long-output timeout into
    its underlying :class:`AgentLoop`.
    """
    dev = Developer()
    assert dev._loop._per_call_timeout_s == LONG_OUTPUT_TIMEOUT_S


def test_explicit_short_timeout_still_honoured() -> None:
    """Reviewer-class call sites can still opt into a tighter budget."""
    loop = AgentLoop(per_call_timeout_s=30.0)
    assert loop._per_call_timeout_s == 30.0
