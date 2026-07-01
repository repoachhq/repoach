"""Unit tests for SP-REVIEW-MEMORY — review-scoped recall + curated seeds.

Mirrors the builder-memory test pattern: pure rendering is tested
directly; recall/seed gating is tested by patching the agentmemory
client and resetting the cached ``Settings`` so the
``REVIEW_MEMORY_ENABLED`` env is re-read. The orchestrator/reviewer
injection is asserted on the rendered prompt — no live network
anywhere (default-network sentinel).
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

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
    AgentLoop — these tests target memory wiring, not auth.
    """
    monkeypatch.setattr(
        "ferova.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://localhost:8082",
            llm_proxy_auth_token=SecretStr("test-token"),
        ),
    )


def test_lessons_section_empty_is_blank() -> None:
    assert review_memory.review_lessons_section([]) == ""


def test_lessons_section_renders_block() -> None:
    out = review_memory.review_lessons_section(["one", "two"])
    assert "## Review lessons (agentmemory)" in out
    assert "verify before you flag" in out
    assert "- one" in out
    assert "- two" in out


def test_recall_disabled_is_noop(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    monkeypatch.setenv("REVIEW_MEMORY_ENABLED", "false")
    calls = {"n": 0}

    def _spy(*args: Any, **kwargs: Any) -> list[str]:
        calls["n"] += 1
        return ["should not appear"]

    monkeypatch.setattr(review_memory.agentmemory_client, "recall", _spy)

    assert review_memory.recall_review_lessons("q") == []
    assert calls["n"] == 0


def test_recall_enabled_calls_client_with_review_project(
    monkeypatch: pytest.MonkeyPatch, fresh_settings: None
) -> None:
    monkeypatch.setenv("REVIEW_MEMORY_ENABLED", "true")
    seen: dict[str, Any] = {}

    def _fake_recall(query: str, *, project: str, base_url: str, **kwargs: Any) -> list[str]:
        seen["project"] = project
        seen["query"] = query
        return ["recalled trap"]

    monkeypatch.setattr(review_memory.agentmemory_client, "recall", _fake_recall)

    assert review_memory.recall_review_lessons("q") == ["recalled trap"]
    assert seen["project"] == "review"


def test_seed_writes_all_lessons(monkeypatch: pytest.MonkeyPatch, fresh_settings: None) -> None:
    written: list[tuple[str, str]] = []

    def _fake_remember(content: str, *, project: str, base_url: str, **kwargs: Any) -> bool:
        written.append((project, content))
        return True

    monkeypatch.setattr(review_memory.agentmemory_client, "remember", _fake_remember)

    assert review_memory.seed_review_memory() == len(review_memory.SEED_REVIEW_LESSONS)
    assert len(written) == 6
    assert all(project == "review" for project, _ in written)


def test_config_kill_switch_defaults_true(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in ("FEROVA_REVIEW_MEMORY_ENABLED", "REVIEW_MEMORY_ENABLED"):
        monkeypatch.delenv(key, raising=False)
    settings = config.Settings(_env_file=None)
    assert settings.review_memory_enabled is True


def test_build_review_lessons_block_queries_title_and_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    def _fake_recall(query: str) -> list[str]:
        seen["query"] = query
        return ["trap lesson"]

    monkeypatch.setattr(orchestrator_module, "recall_review_lessons", _fake_recall)

    diff = (
        "diff --git a/src/ferova/foo.py b/src/ferova/foo.py\n"
        "+++ b/src/ferova/foo.py\n"
        "diff --git a/tests/unit/test_foo.py b/tests/unit/test_foo.py\n"
    )
    block = orchestrator_module._build_review_lessons_block("My PR title", diff)
    assert "trap lesson" in block
    assert "My PR title" in seen["query"]
    assert "src/ferova/foo.py" in seen["query"]
    assert "tests/unit/test_foo.py" in seen["query"]


def test_build_review_lessons_block_empty_recall_is_blank(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(orchestrator_module, "recall_review_lessons", lambda _q: [])
    assert orchestrator_module._build_review_lessons_block("t", "") == ""


def test_orchestrator_appends_lessons_to_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake_call(self: Scribe, prompt: str, *, pr_number: int | None = None) -> Any:
        captured["prompt"] = prompt
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(Scribe, "_call_with_retry", _fake_call)

    section = review_memory.review_lessons_section(["never flag pre-existing code"])
    with pytest.raises(RuntimeError, match="stop after prompt capture"):
        Scribe().review_diff("diff --git a/x b/x", extra_prompt_section=section)

    assert captured["prompt"].endswith(section)
    assert "never flag pre-existing code" in captured["prompt"]


def test_review_diff_without_section_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, str] = {}

    def _fake_call(self: Scribe, prompt: str, *, pr_number: int | None = None) -> Any:
        captured["prompt"] = prompt
        raise RuntimeError("stop after prompt capture")

    monkeypatch.setattr(Scribe, "_call_with_retry", _fake_call)

    with pytest.raises(RuntimeError):
        Scribe().review_diff("diff --git a/x b/x")

    assert "## Review lessons (agentmemory)" not in captured["prompt"]


def test_cli_commands_call_correct_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    from ferova.cli.main import app

    monkeypatch.setattr("ferova.review.review_memory.seed_review_memory", lambda: 6)
    monkeypatch.setattr(
        "ferova.review.review_memory.recall_review_lessons",
        lambda query: [f"lesson for {query}"],
    )

    runner = CliRunner()
    seed_result = runner.invoke(app, ["memory", "seed-review"])
    assert seed_result.exit_code == 0
    assert "seeded 6 review lesson(s)" in seed_result.output

    recall_result = runner.invoke(app, ["memory", "recall-review", "docstring"])
    assert recall_result.exit_code == 0
    assert "lesson for docstring" in recall_result.output
