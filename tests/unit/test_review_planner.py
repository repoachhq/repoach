"""SP-PLANNER-AGENT — Planner agent + run_planner_session.

Drives the Planner with an injected fake AgentLoop (no network): the
happy path writes a parseable plan document; every failure mode (no
payload, invalid payload, wrong spec id, missing spec) is a loud
error with nothing written. Also pins the persona file's placeholders
and output-contract vocabulary, and the prompt assembly (spec +
repo tree actually injected).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from repoach.agent_engine.agent_loop import NimAgentOutput
from repoach.review.plan import PLAN_MARKER, load_plan
from repoach.review.planner import Planner, run_planner_session

_SPEC_ID = "SP-TEST-PLANNER"


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


class _ScriptedLoop:
    """AgentLoop stand-in replaying a different output per ``run`` call."""

    def __init__(self, texts: list[str]) -> None:
        self._outputs = [
            NimAgentOutput(
                text=t,
                tool_calls_made=["list_dir"] if i == 0 else [],
                elapsed_s=1.0,
                tokens_used=100,
                model_used="fake/coder",
                turns=1,
            )
            for i, t in enumerate(texts)
        ]
        self.calls: list[dict] = []

    def run(self, prompt: str, *, system: str | None = None, tools: list | None = None):
        self.calls.append({"prompt": prompt, "tools": tools or []})
        return self._outputs[min(len(self.calls) - 1, len(self._outputs) - 1)]


def _valid_plan_payload() -> dict:
    return {
        "spec_id": _SPEC_ID,
        "title": "Test plan",
        "summary": "Plan emitted by the fake loop.",
        "steps": [
            {
                "index": 1,
                "title": "Add the module",
                "files": [
                    "src/repoach/demo.py",
                    "tests/unit/test_demo.py",
                    "tests/integration/test_demo_flow.py",
                ],
                "action": "Create the module.",
                "commit_message": "feat(demo): add module",
                "done_when": "pytest tests/unit/test_demo.py is green",
                "unit_tests": ["tests/unit/test_demo.py::test_happy_path_writes_parseable_plan"],
            }
        ],
        "integration_tests": ["tests/integration/test_demo_flow.py"],
    }


def _repo_with_spec(tmp_path: Path) -> Path:
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / f"2026-06-07_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    return tmp_path


def _planner_with(text: str, repo: Path, **output_kwargs) -> tuple[Planner, _FakeLoop]:
    loop = _FakeLoop(
        NimAgentOutput(
            text=text,
            tool_calls_made=["list_dir", "read_file"],
            elapsed_s=1.5,
            tokens_used=420,
            model_used="fake/opus",
            turns=3,
            **output_kwargs,
        )
    )
    return Planner(loop=loop, repo_root=repo), loop


class TestRunPlannerSession:
    def test_happy_path_writes_parseable_plan(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        text = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
        planner, _loop = _planner_with(text, repo)

        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

        assert outcome.error is None
        assert outcome.written is True
        assert outcome.plan_path == f"docs/plans/{_SPEC_ID}.md"
        assert outcome.n_steps == 1
        assert outcome.tool_calls == ["list_dir", "read_file"]
        assert outcome.turns == 3
        assert outcome.tokens_used == 420
        assert outcome.model_used == "fake/opus"
        document = (repo / outcome.plan_path).read_text(encoding="utf-8")
        assert PLAN_MARKER in document
        assert load_plan(_SPEC_ID, root=repo).spec_id == _SPEC_ID

    def test_prompt_assembly_injects_spec_and_tools(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        text = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
        planner, loop = _planner_with(text, repo)

        run_planner_session(_SPEC_ID, root=repo, planner=planner)

        assert "demo spec" in loop.system
        assert "{SPEC_PLAN}" not in loop.system
        assert "{REPO_TREE}" not in loop.system
        assert _SPEC_ID in loop.prompt
        assert {t.name for t in loop.tools} == {"list_dir", "read_file", "grep_repo"}

    def test_last_json_fence_wins(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        text = (
            'Earlier draft:\n```json\n{"spec_id": "SP-WRONG"}\n```\n'
            f"Final plan:\n```json\n{json.dumps(_valid_plan_payload())}\n```"
        )
        planner, _ = _planner_with(text, repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is True

    def test_json_embedded_in_prose_without_fence_salvaged(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        text = (
            "I explored the repository thoroughly. Here is the plan I propose:\n\n"
            f"{json.dumps(_valid_plan_payload())}\n\n"
            "Let me know if you would like me to adjust any step."
        )
        planner, _ = _planner_with(text, repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is True


class TestJsonExtraction:
    def test_braces_inside_string_values_do_not_break_balance(self) -> None:
        from repoach.review.planner import _extract_plan_json

        payload = dict(_valid_plan_payload())
        payload["steps"][0]["done_when"] = "ruff check {src} exits 0 and {tests}"
        text = f"Preamble prose.\n{json.dumps(payload)}\nTrailing words."
        raw = _extract_plan_json(text)
        assert raw is not None
        assert json.loads(raw)["steps"][0]["done_when"] == "ruff check {src} exits 0 and {tests}"

    def test_only_plan_shaped_object_is_picked(self) -> None:
        from repoach.review.planner import _extract_plan_json

        text = (
            'First a config blob {"foo": 1, "bar": 2} then the real one:\n'
            f"{json.dumps(_valid_plan_payload())}"
        )
        raw = _extract_plan_json(text)
        assert raw is not None
        assert json.loads(raw)["spec_id"] == _SPEC_ID

    def test_prose_with_no_json_returns_none(self) -> None:
        from repoach.review.planner import _extract_plan_json

        assert _extract_plan_json("I could not produce a plan, sorry.") is None

    def test_bare_json_without_fence_accepted(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        planner, _ = _planner_with(json.dumps(_valid_plan_payload()), repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is True

    def test_no_payload_is_loud_and_writes_nothing(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        planner, _ = _planner_with("I could not decide on a plan, sorry.", repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is False
        assert outcome.error is not None
        assert "no JSON plan payload" in outcome.error
        assert not (repo / "docs" / "plans").exists()

    def test_invalid_payload_is_loud(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload(), steps=[])
        planner, _ = _planner_with(f"```json\n{json.dumps(bad)}\n```", repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is False
        assert "validation" in (outcome.error or "")

    def test_spec_id_mismatch_rejects_plan(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        wrong = dict(_valid_plan_payload(), spec_id="SP-SOMETHING-ELSE")
        planner, _ = _planner_with(f"```json\n{json.dumps(wrong)}\n```", repo)
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)
        assert outcome.written is False
        assert "SP-SOMETHING-ELSE" in (outcome.error or "")

    def test_missing_spec_short_circuits(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "specs").mkdir(parents=True)
        outcome = run_planner_session("SP-ABSENT", root=tmp_path)
        assert outcome.written is False
        assert "spec not found" in (outcome.error or "")


class TestProxyGracefulFailure:
    """The proxy path must fail SAFE on a gateway/chain failure — a loud
    error outcome, never a propagated crash (brain-swap experiment,
    2026-06-09: a degraded NIM coder chain crashed the proxy Planner)."""

    class _RaisingLoop:
        def __init__(self, raise_on_call: int) -> None:
            self._raise_on = raise_on_call
            self.calls = 0

        def run(self, prompt, *, system=None, tools=None):
            from repoach.agent_engine.adapters import GatewayTransportError

            self.calls += 1
            if self.calls == self._raise_on:
                raise GatewayTransportError("proxy returned 502: all candidates empty")
            return NimAgentOutput(
                text="not a plan",
                tool_calls_made=["list_dir"],
                elapsed_s=1.0,
                tokens_used=10,
                model_used="fake/coder",
                turns=1,
            )

    def test_exploration_gateway_error_returns_outcome_not_crash(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        planner = Planner(loop=self._RaisingLoop(raise_on_call=1), repo_root=repo)

        plan, error, audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert "proxy exploration failed" in error
        assert audit["explore_via"] == "proxy"

    def test_refinement_gateway_error_returns_outcome_not_crash(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        planner = Planner(loop=self._RaisingLoop(raise_on_call=2), repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert "proxy refinement failed" in error

    def test_session_reports_gateway_failure_gracefully(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        planner = Planner(loop=self._RaisingLoop(raise_on_call=1), repo_root=repo)

        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

        assert outcome.written is False
        assert "proxy exploration failed" in (outcome.error or "")


class TestPlanRetry:
    def test_invalid_then_valid_recovers_without_re_exploring(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload())
        del bad["steps"][0]["commit_message"]
        valid = _valid_plan_payload()
        loop = _ScriptedLoop(
            [
                f"```json\n{json.dumps(bad)}\n```",
                f"```json\n{json.dumps(valid)}\n```",
            ]
        )
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert error is None
        assert plan is not None
        assert len(loop.calls) == 2
        assert loop.calls[0]["tools"]
        assert not loop.calls[1]["tools"]
        assert "REJECTED" in loop.calls[1]["prompt"]

    def test_three_invalid_attempts_give_up_loudly(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FEROVA_PLANNER_PARSE_ATTEMPTS", "3")
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload())
        del bad["steps"][0]["commit_message"]
        loop = _ScriptedLoop([f"```json\n{json.dumps(bad)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert "validation" in error
        assert len(loop.calls) == 3

    def test_first_attempt_valid_makes_one_call(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        loop = _ScriptedLoop([f"```json\n{json.dumps(_valid_plan_payload())}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert error is None
        assert plan is not None
        assert len(loop.calls) == 1


class TestClaudeCliMode:
    @staticmethod
    def _cc_result(text: str, *, is_error: bool = False):
        from repoach.review.planner_cc import CcExploreResult

        return CcExploreResult(
            text=text,
            num_turns=4,
            duration_ms=8000,
            is_error=is_error,
            error="claude -p timed out after 600s" if is_error else None,
        )

    def test_cc_mode_produces_plan_and_tags_audit(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        valid = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
        calls: list[dict] = []

        def fake_cc(*, prompt, repo_root, model, allow_tools, **kw):
            calls.append({"allow_tools": allow_tools, "model": model})
            return self._cc_result(valid)

        planner = Planner(repo_root=repo, explore_via="claude_cli", cc_model="opus")
        with patch("repoach.review.planner.run_cc_exploration", side_effect=fake_cc):
            plan, error, audit = planner.plan(
                spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="unused"
            )

        assert error is None
        assert plan is not None
        assert audit["explore_via"] == "claude_cli"
        assert audit["model_used"] == "claude-cli/opus"
        assert len(calls) == 1
        assert calls[0]["allow_tools"] is True
        assert calls[0]["model"] == "opus"

    def test_cc_mode_refinement_disables_tools(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload())
        del bad["steps"][0]["commit_message"]
        outputs = [
            self._cc_result(f"```json\n{json.dumps(bad)}\n```"),
            self._cc_result(f"```json\n{json.dumps(_valid_plan_payload())}\n```"),
        ]
        calls: list[bool] = []

        def fake_cc(*, prompt, repo_root, model, allow_tools, **kw):
            calls.append(allow_tools)
            return outputs[len(calls) - 1]

        planner = Planner(repo_root=repo, explore_via="claude_cli")
        with patch("repoach.review.planner.run_cc_exploration", side_effect=fake_cc):
            plan, _error, _audit = planner.plan(
                spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="unused"
            )

        assert plan is not None
        assert calls == [True, False]

    def test_cc_exploration_error_is_loud(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)

        def fake_cc(**kw):
            return self._cc_result("", is_error=True)

        planner = Planner(repo_root=repo, explore_via="claude_cli")
        with patch("repoach.review.planner.run_cc_exploration", side_effect=fake_cc):
            plan, error, _audit = planner.plan(
                spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="unused"
            )

        assert plan is None
        assert "claude exploration failed" in (error or "")


class TestCcPersonaFile:
    _persona = Path(__file__).resolve().parents[2] / "prompts" / "review" / "planner_cc_0.1.1.md"

    def test_cc_persona_exists_with_spec_placeholder(self) -> None:
        text = self._persona.read_text(encoding="utf-8")
        assert "{SPEC_PLAN}" in text
        assert "{REPO_TREE}" not in text

    def test_cc_persona_describes_native_tools_and_contract(self) -> None:
        text = self._persona.read_text(encoding="utf-8")
        assert "Read" in text and "Glob" in text and "Grep" in text
        for token in ("spec_id", "done_when", "unit_tests", "integration_tests"):
            assert token in text


class TestPersonaInjection:
    def test_missing_persona_file_raises_file_not_found(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        empty_prompts = tmp_path / "empty_prompts"
        empty_prompts.mkdir()
        planner, _ = _planner_with("irrelevant", repo)
        planner._prompts_dir = empty_prompts
        with pytest.raises(FileNotFoundError):
            planner.plan(spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/")

    def test_persona_without_placeholders_still_runs_but_keeps_literals_out(
        self, tmp_path: Path
    ) -> None:
        repo = _repo_with_spec(tmp_path)
        prompts = tmp_path / "prompts"
        prompts.mkdir()
        (prompts / "planner_0.2.1.md").write_text(
            "You are Planner. {SPEC_PLAN} {REPO_TREE}", encoding="utf-8"
        )
        text = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
        planner, loop = _planner_with(text, repo)
        planner._prompts_dir = prompts
        plan, error, _ = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# the spec body", repo_tree="src/"
        )
        assert error is None
        assert plan is not None
        assert "the spec body" in loop.system
        assert "{SPEC_PLAN}" not in loop.system


class TestPersonaFile:
    _persona = Path(__file__).resolve().parents[2] / "prompts" / "review" / "planner_0.2.1.md"

    def test_persona_exists_with_placeholders(self) -> None:
        text = self._persona.read_text(encoding="utf-8")
        assert "{SPEC_PLAN}" in text
        assert "{REPO_TREE}" in text

    def test_persona_documents_the_actionplan_contract(self) -> None:
        text = self._persona.read_text(encoding="utf-8")
        for token in (
            "spec_id",
            "done_when",
            "unit_tests",
            "integration_tests",
            "commit_message",
        ):
            assert token in text, f"persona lost the {token!r} contract vocabulary"

    def test_planner_class_defaults(self) -> None:
        assert Planner.persona_filename == "planner_0.2.1.md"
        assert Planner.role.value == "planner"


class TestSelectorCheck:
    """Mechanical selector verification in the Planner's refine loop."""

    def test_hallucinated_selector_in_existing_file_is_refined(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        (repo / "tests" / "unit").mkdir(exist_ok=True)
        (repo / "tests" / "unit" / "test_existing.py").write_text(
            "def test_real():\n    assert True\n", encoding="utf-8"
        )
        payload = _valid_plan_payload()
        payload["steps"][0]["files"][1] = "tests/unit/test_existing.py"
        payload["steps"][0]["unit_tests"] = ["tests/unit/test_existing.py::test_fake"]
        loop = _ScriptedLoop([f"```json\n{json.dumps(payload)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert "tests/unit/test_existing.py::test_fake" in error
        assert "selector_present" in error
        assert "node id" in error

    def test_resolved_selector_is_accepted(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        (repo / "tests" / "unit").mkdir(exist_ok=True)
        (repo / "tests" / "unit" / "test_existing.py").write_text(
            "def test_real():\n    assert True\n", encoding="utf-8"
        )
        payload = _valid_plan_payload()
        payload["steps"][0]["files"][1] = "tests/unit/test_existing.py"
        payload["steps"][0]["unit_tests"] = ["tests/unit/test_existing.py::test_real"]
        loop = _ScriptedLoop([f"```json\n{json.dumps(payload)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert error is None
        assert plan is not None

    def test_declared_creation_in_action_text_is_accepted(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        (repo / "tests" / "unit").mkdir(exist_ok=True)
        (repo / "tests" / "unit" / "test_existing.py").write_text(
            "def test_real():\n    assert True\n", encoding="utf-8"
        )
        payload = _valid_plan_payload()
        payload["steps"][0]["files"][1] = "tests/unit/test_existing.py"
        payload["steps"][0]["unit_tests"] = ["tests/unit/test_existing.py::test_new"]
        payload["steps"][0]["action"] = "Create test_new in test_existing.py."
        loop = _ScriptedLoop([f"```json\n{json.dumps(payload)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert error is None
        assert plan is not None

    def test_selector_in_new_file_is_exempt(self, tmp_path: Path) -> None:
        repo = _repo_with_spec(tmp_path)
        payload = _valid_plan_payload()
        payload["steps"][0]["unit_tests"] = ["tests/unit/test_new.py::test_new"]
        payload["steps"][0]["files"][1] = "tests/unit/test_new.py"
        loop = _ScriptedLoop([f"```json\n{json.dumps(payload)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert error is None
        assert plan is not None


class TestErrorHistory:
    """SP-PLANNER-REFINE-HISTORY — full error history in the refine loop."""

    def test_refine_prompt_carries_full_error_history(self) -> None:
        from repoach.review.planner import _refine_prompt

        errors = ["first failure: missing commit_message", "second failure: bad selector"]
        prompt = _refine_prompt("previous candidate text", errors)

        assert "1. first failure: missing commit_message" in prompt
        assert "2. second failure: bad selector" in prompt
        assert prompt.index("first failure") < prompt.index("second failure")

    def test_single_error_history_matches_previous_behaviour(self) -> None:
        from repoach.review.planner import _refine_prompt

        prompt = _refine_prompt("previous candidate text", ["only failure"])

        assert "1. only failure" in prompt
        assert "REJECTED" in prompt
        assert "previous candidate text" in prompt

    def test_parse_attempts_setting_is_honored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FEROVA_PLANNER_PARSE_ATTEMPTS", "2")
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload())
        del bad["steps"][0]["commit_message"]
        loop = _ScriptedLoop([f"```json\n{json.dumps(bad)}\n```"])
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert len(loop.calls) == 2

    def test_exhausted_session_reports_full_history(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("FEROVA_PLANNER_PARSE_ATTEMPTS", "3")
        repo = _repo_with_spec(tmp_path)
        bad_a = dict(_valid_plan_payload())
        del bad_a["steps"][0]["commit_message"]
        bad_b = dict(_valid_plan_payload(), steps=[])
        bad_c = dict(_valid_plan_payload(), spec_id="SP-WRONG")
        loop = _ScriptedLoop(
            [
                f"```json\n{json.dumps(bad_a)}\n```",
                f"```json\n{json.dumps(bad_b)}\n```",
                f"```json\n{json.dumps(bad_c)}\n```",
            ]
        )
        planner = Planner(loop=loop, repo_root=repo)

        plan, error, _audit = planner.plan(
            spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
        )

        assert plan is None
        assert error is not None
        assert "validation" in error
        assert "SP-WRONG" in error
        assert len(loop.calls) == 3

    def test_parse_attempts_below_one_is_clamped(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A setting below 1 clamps to 1: initial attempt only, no refinement."""
        repo = _repo_with_spec(tmp_path)
        bad = dict(_valid_plan_payload())
        del bad["steps"][0]["commit_message"]

        for value in ("0", "-1"):
            monkeypatch.setenv("FEROVA_PLANNER_PARSE_ATTEMPTS", value)
            loop = _ScriptedLoop([f"```json\n{json.dumps(bad)}\n```"])
            planner = Planner(loop=loop, repo_root=repo)

            plan, error, _audit = planner.plan(
                spec_id=_SPEC_ID, spec_markdown="# spec", repo_tree="src/"
            )

            assert plan is None
            assert error is not None
            assert len(loop.calls) == 1
