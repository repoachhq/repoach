"""Tests for SP-DEVAGENT-LOOP (slice 2): the agentic author→test→iterate driver.

Focus: ``run_agentic_step`` assembles the read + author/verify toolbox and drives
``AgentLoop.run`` (a FakeLoop captures the call, mirroring the Planner tests),
maps the loop output into a :class:`DevLoopResult`, jails the write tools to the
step's ``allowed_paths``, and turns a brain/proxy outage into an inert error result
instead of a crash.
"""

from __future__ import annotations

from pathlib import Path

from ferova.agent_engine.adapters import GatewayError
from ferova.agent_engine.agent_loop import NimAgentOutput
from ferova.review.devagent_loop import DevLoopResult, run_agentic_step


class _FakeLoop:
    """AgentLoop stand-in capturing the call and replaying a canned output."""

    def __init__(self, output: NimAgentOutput) -> None:
        self.output = output
        self.prompt: str = ""
        self.system: str = ""
        self.tools: list = []

    def run(self, prompt: str, *, system: str | None = None, tools: list | None = None):
        self.prompt = prompt
        self.system = system or ""
        self.tools = tools or []
        return self.output


class _BoomLoop:
    """AgentLoop stand-in that raises a gateway error on run."""

    def run(self, prompt: str, *, system: str | None = None, tools: list | None = None):
        raise GatewayError("chain exhausted")


def test_assembles_read_and_author_tools_and_maps_output(tmp_path: Path) -> None:
    out = NimAgentOutput(
        text="done",
        tool_calls_made=["read_file", "write_file", "run_tests"],
        turns=3,
        tokens_used=42,
        model_used="fake/coder",
        elapsed_s=1.5,
    )
    loop = _FakeLoop(out)

    result = run_agentic_step(
        loop,
        system="persona",
        brief="implement the step",
        repo_root=tmp_path,
        allowed_paths=["src/x.py"],
    )

    assert loop.system == "persona"
    tool_names = {tool.name for tool in loop.tools}
    assert {
        "list_dir",
        "read_file",
        "grep_repo",
        "write_file",
        "edit_file",
        "run_tests",
        "run_ruff",
    } <= tool_names
    assert isinstance(result, DevLoopResult)
    assert result.text == "done"
    assert result.turns == 3
    assert result.tokens_used == 42
    assert result.model_used == "fake/coder"
    assert result.tool_calls_made == ["read_file", "write_file", "run_tests"]
    assert result.error is None


def test_repo_tree_is_appended_to_brief(tmp_path: Path) -> None:
    loop = _FakeLoop(NimAgentOutput(text="ok"))

    run_agentic_step(
        loop,
        system="s",
        brief="THE-BRIEF",
        repo_root=tmp_path,
        allowed_paths=[],
        repo_tree="src/\n  x.py\n",
    )

    assert "THE-BRIEF" in loop.prompt
    assert "Repository tree" in loop.prompt
    assert "x.py" in loop.prompt


def test_allowed_paths_jail_is_wired_into_the_write_tools(tmp_path: Path) -> None:
    loop = _FakeLoop(NimAgentOutput(text="ok"))

    run_agentic_step(
        loop,
        system="s",
        brief="b",
        repo_root=tmp_path,
        allowed_paths=["src/keep.py"],
    )

    write = {tool.name: tool.callable_fn for tool in loop.tools}["write_file"]
    assert write("src/keep.py", "x = 1\n").startswith("ok")
    refused = write("src/other.py", "y = 1\n")
    assert refused.startswith("error")
    assert "contract" in refused
    assert not (tmp_path / "src" / "other.py").exists()


def test_gateway_error_returns_inert_result(tmp_path: Path) -> None:
    result = run_agentic_step(
        _BoomLoop(),
        system="s",
        brief="b",
        repo_root=tmp_path,
        allowed_paths=[],
    )

    assert result.error is not None
    assert "chain exhausted" in result.error
    assert result.text == ""
    assert result.tool_calls_made == []
