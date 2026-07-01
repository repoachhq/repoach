"""Unit tests for SP-BUILDER-MEMORY — builder-scoped orchestration.

Pure rendering/composition is tested directly; the recall/remember
gating is tested by patching the client and resetting the cached
``Settings`` so the ``BUILDER_MEMORY_ENABLED`` env is re-read.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

import ferova.core.config as config
from ferova.review import builder_memory


@pytest.fixture()
def fresh_settings() -> Iterator[None]:
    config._settings = None
    try:
        yield
    finally:
        config._settings = None


def test_lessons_section_empty_is_blank() -> None:
    assert builder_memory.lessons_section([]) == ""


def test_lessons_section_renders_block() -> None:
    out = builder_memory.lessons_section(["one", "two"])
    assert "## Lessons from past builds (agentmemory)" in out
    assert "- one" in out
    assert "- two" in out


def test_recall_disabled_is_noop(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "false")
    calls = {"n": 0}

    def _spy(*args: Any, **kwargs: Any) -> list[str]:
        calls["n"] += 1
        return ["should not appear"]

    monkeypatch.setattr(builder_memory.agentmemory_client, "recall", _spy)

    assert builder_memory.recall_builder_lessons("q") == []
    assert calls["n"] == 0


def test_recall_enabled_calls_client_with_builder_project(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "true")
    seen: dict[str, Any] = {}

    def _fake_recall(query: str, *, project: str, base_url: str, **kwargs: Any) -> list[str]:
        seen["project"] = project
        return ["recalled lesson"]

    monkeypatch.setattr(builder_memory.agentmemory_client, "recall", _fake_recall)

    assert builder_memory.recall_builder_lessons("q") == ["recalled lesson"]
    assert seen["project"] == "builder"


def test_remember_outcome_pushed(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "true")
    captured: dict[str, Any] = {}

    def _fake_remember(content: str, *, project: str, base_url: str, **kwargs: Any) -> bool:
        captured["content"] = content
        captured["project"] = project
        return True

    monkeypatch.setattr(builder_memory.agentmemory_client, "remember", _fake_remember)

    assert (
        builder_memory.remember_build_outcome("SP-X", pushed=True, no_op_reason=None, n_steps=3)
        is True
    )
    assert "SP-X" in captured["content"]
    assert "pushed" in captured["content"]
    assert captured["project"] == "builder"


def test_remember_outcome_stalled(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "true")
    captured: dict[str, Any] = {}

    def _fake_remember(content: str, *, project: str, base_url: str, **kwargs: Any) -> bool:
        captured["content"] = content
        return True

    monkeypatch.setattr(builder_memory.agentmemory_client, "remember", _fake_remember)

    builder_memory.remember_build_outcome(
        "SP-Y", pushed=False, no_op_reason="step 1 missing test", n_steps=1
    )
    assert "stalled" in captured["content"]
    assert "step 1 missing test" in captured["content"]


def test_remember_disabled_is_noop(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "false")

    def _boom(*args: Any, **kwargs: Any) -> bool:
        raise AssertionError("client must not be called when disabled")

    monkeypatch.setattr(builder_memory.agentmemory_client, "remember", _boom)

    assert (
        builder_memory.remember_build_outcome("SP-Z", pushed=True, no_op_reason=None, n_steps=2)
        is False
    )


def test_seed_writes_all_lessons(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("BUILDER_MEMORY_ENABLED", "true")
    monkeypatch.setattr(builder_memory.agentmemory_client, "remember", lambda *a, **k: True)

    assert builder_memory.seed_builder_memory() == len(builder_memory.SEED_LESSONS)
