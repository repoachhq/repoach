"""Integration test for SP-REVIEW-MEMORY — seed → recall → prompt injection.

Exercises the whole review-memory flow against a fake in-process
agentmemory store (no live service, no network): seeding writes the
curated traps into the review scope, the orchestrator-level block
builder recalls them, and the rendered section lands at the end of a
reviewer prompt. Also pins the kill-switch end to end.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

import ferova.core.config as config
import ferova.review.orchestrator as orchestrator_module
from ferova.review import review_memory
from ferova.review.reviewer import Scribe


@pytest.fixture()
def fresh_settings() -> Iterator[None]:
    config._settings = None
    try:
        yield
    finally:
        config._settings = None


@pytest.fixture(autouse=True)
def _stub_proxy_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep reviewer construction possible without the operator ``.env``.

    SP-PROXY-SECURE-DEFAULTS removed the implicit token fallback, so a
    tokenless environment (CI) refuses to build the underlying
    AgentLoop — this flow targets memory wiring, not auth.
    """
    monkeypatch.setattr(
        "ferova.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


@pytest.fixture()
def fake_store(monkeypatch: pytest.MonkeyPatch) -> dict[str, list[str]]:
    store: dict[str, list[str]] = {}

    def _remember(content: str, *, project: str, base_url: str, **kwargs: Any) -> bool:
        store.setdefault(project, []).append(content)
        return True

    def _recall(query: str, *, project: str, base_url: str, **kwargs: Any) -> list[str]:
        return [item for item in store.get(project, []) if item][:5]

    monkeypatch.setattr(review_memory.agentmemory_client, "remember", _remember)
    monkeypatch.setattr(review_memory.agentmemory_client, "recall", _recall)
    return store


def test_seed_recall_inject_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings: None,
    fake_store: dict[str, list[str]],
) -> None:
    monkeypatch.setenv("REVIEW_MEMORY_ENABLED", "true")

    assert review_memory.seed_review_memory() == 6
    assert len(fake_store["ferova-review"]) == 6

    diff = "diff --git a/src/ferova/review/foo.py b/src/ferova/review/foo.py\n"
    block = orchestrator_module._build_review_lessons_block("fix review foo", diff)
    assert "## Review lessons (agentmemory)" in block

    captured: dict[str, str] = {}

    def _fake_call(self: Scribe, prompt: str, *, pr_number: int | None = None) -> Any:
        captured["prompt"] = prompt
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(Scribe, "_call_with_retry", _fake_call)
    with pytest.raises(RuntimeError):
        Scribe().review_diff(diff, extra_prompt_section=block)

    assert captured["prompt"].endswith(block)


def test_kill_switch_disables_recall_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    fresh_settings: None,
    fake_store: dict[str, list[str]],
) -> None:
    fake_store["ferova-review"] = ["a lesson that must not surface"]
    monkeypatch.setenv("REVIEW_MEMORY_ENABLED", "false")

    block = orchestrator_module._build_review_lessons_block("title", "")
    assert block == ""
