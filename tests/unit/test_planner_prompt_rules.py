"""SP-PLAN-QUALITY step 3 — full rule catalog in the Planner loop, strict gate.

Pins three contracts on top of the rule catalog (step 1) and the
strict production-time layer (step 2):

* the INITIAL planning prompt carries the fixed heading and the full
  rendered catalog, reached through the real ``run_planner_session`` /
  ``Planner`` prompt-assembly path (spec_markdown augmentation feeding
  ``_spec_block``);
* every REFINE turn keeps the existing full error history exactly as
  before AND carries the same catalog;
* a payload that passes ``ActionPlan`` pydantic validation but violates
  the strict layer (:func:`repoach.review.plan.validate_plan_form_strict`)
  is refused and refined exactly like a ``plan_invalid`` failure, never
  written;
* ``_parse_attempts`` reads ``REPOACH_PLANNER_PARSE_ATTEMPTS`` with the
  documented clamp-to-1 and fallback-to-5 behavior.

The recording fake loop follows the ``_ScriptedLoop`` pattern from
``tests/integration/test_planner_refine_history.py`` — no stubs of
Planner internals, only a fake transport at the ``AgentLoop`` boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.agent_engine.agent_loop import NimAgentOutput
from repoach.review.plan import PLAN_STEP_MAX_FILES, render_plan_form_rules
from repoach.review.planner import Planner, _parse_attempts, run_planner_session

_SPEC_ID = "SP-TEST-PROMPT-RULES"
_HEADING = "Plan-form rules (all of them — every attempt is validated against every rule)"


def _valid_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a minimal valid plan payload the session will accept."""
    return {
        "spec_id": spec_id,
        "title": "Prompt rules test plan",
        "summary": "Exercises the full rule-catalog injection and the strict emission gate.",
        "steps": [
            {
                "index": 1,
                "title": "Add the demo module",
                "files": [
                    "src/repoach/prompt_rules_demo.py",
                    "tests/unit/test_prompt_rules_demo.py",
                    "tests/integration/test_prompt_rules_demo_flow.py",
                ],
                "action": "Create the demo module and wire it into the review package.",
                "commit_message": "feat(demo): add prompt rules demo module",
                "done_when": "pytest tests/unit/test_prompt_rules_demo.py is green",
                "unit_tests": ["tests/unit/test_prompt_rules_demo.py::test_happy_path"],
            }
        ],
        "integration_tests": ["tests/integration/test_prompt_rules_demo_flow.py"],
    }


def _oversized_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a payload that passes pydantic validation but trips the size cap."""
    payload = _valid_plan_payload(spec_id)
    payload["steps"][0]["files"] = [
        "src/repoach/a.py",
        "src/repoach/b.py",
        "src/repoach/c.py",
        "src/repoach/d.py",
        "tests/unit/test_prompt_rules_demo.py",
        "tests/integration/test_prompt_rules_demo_flow.py",
    ]
    return payload


def _seed_repo(tmp_path: Path) -> Path:
    """Seed a tmp repo with a spec doc the session can load."""
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / f"2026-07-08_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    return tmp_path


def _catalog_rule_sentences(minimum: int) -> list[str]:
    """Return at least *minimum* rule sentences pulled from the real catalog."""
    catalog = render_plan_form_rules()
    sentences = [line.split(". ", 1)[1] for line in catalog.splitlines() if line.strip()]
    assert len(sentences) >= minimum
    return sentences[:minimum]


class _ScriptedLoop:
    """AgentLoop stand-in replaying a different output per ``run`` call.

    The exploration call (index 0, with tools) always returns the
    first scripted text; every subsequent refinement call (no tools,
    indices 1+) returns the next scripted text. Every call's prompt,
    system, and tools are recorded for assertion.
    """

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
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, *, system: str | None = None, tools: list | None = None):
        idx = min(len(self.calls), len(self._outputs) - 1)
        self.calls.append({"prompt": prompt, "system": system or "", "tools": tools or []})
        return self._outputs[idx]


def test_initial_prompt_carries_full_catalog(tmp_path: Path) -> None:
    """The very first planning prompt carries the fixed heading and the catalog."""
    repo = _seed_repo(tmp_path)
    valid_text = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
    loop = _ScriptedLoop([valid_text])
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert len(loop.calls) == 1

    system = str(loop.calls[0]["system"])
    assert _HEADING in system
    for sentence in _catalog_rule_sentences(3):
        assert sentence in system


def test_refine_prompt_carries_catalog_and_history(tmp_path: Path) -> None:
    """A refine turn keeps the prior error history AND carries the catalog."""
    repo = _seed_repo(tmp_path)

    bad_a = dict(_valid_plan_payload())
    del bad_a["steps"][0]["commit_message"]

    bad_b = dict(_valid_plan_payload(), spec_id="SP-WRONG")

    valid = _valid_plan_payload()

    texts = [
        f"```json\n{json.dumps(bad_a)}\n```",
        f"```json\n{json.dumps(bad_b)}\n```",
        f"```json\n{json.dumps(valid)}\n```",
    ]
    loop = _ScriptedLoop(texts)
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert len(loop.calls) == 3

    refine_prompt = str(loop.calls[2]["prompt"])
    assert "1." in refine_prompt
    assert "2." in refine_prompt
    assert "REJECTED" in refine_prompt
    assert "plan payload failed validation" in refine_prompt
    assert "requested" in refine_prompt

    assert _HEADING in refine_prompt
    for sentence in _catalog_rule_sentences(3):
        assert sentence in refine_prompt


def test_strict_rules_gate_planner_emission(tmp_path: Path) -> None:
    """A payload violating the size cap is refused and refined; the clean one is written."""
    repo = _seed_repo(tmp_path)

    oversized = _oversized_plan_payload()
    clean = _valid_plan_payload()

    texts = [
        f"```json\n{json.dumps(oversized)}\n```",
        f"```json\n{json.dumps(clean)}\n```",
    ]
    loop = _ScriptedLoop(texts)
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert len(loop.calls) == 2

    refine_prompt = str(loop.calls[1]["prompt"])
    assert str(PLAN_STEP_MAX_FILES) in refine_prompt
    assert "30-turn" in refine_prompt


def test_attempt_budget_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """REPOACH_PLANNER_PARSE_ATTEMPTS clamps low values to 1; unset/non-integer fall back to 5."""
    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "0")
    assert _parse_attempts() == 1

    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "-5")
    assert _parse_attempts() == 1

    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "1")
    assert _parse_attempts() == 1

    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "7")
    assert _parse_attempts() == 7

    monkeypatch.delenv("REPOACH_PLANNER_PARSE_ATTEMPTS", raising=False)
    assert _parse_attempts() == 5

    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "not-a-number")
    assert _parse_attempts() == 5

    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "   ")
    assert _parse_attempts() == 5
