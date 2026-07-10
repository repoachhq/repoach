"""SP-PLAN-STEP-DENSITY-CAP step 2 — the density cap gates Planner emission.

Drives ``run_planner_session`` end to end against a throwaway governed
spec in a ``tmp_path`` repo with a scripted :class:`AgentLoop`
stand-in (mirrors ``tests/integration/test_planner_refine_history.py``):
the FIRST scripted payload has a step with 2 files and an action
longer than ``2 * PLAN_STEP_MAX_ACTION_DENSITY`` chars — over the
density cap while otherwise valid — so the strict emission layer
refuses it and the session refines; the SECOND scripted payload is a
clean plan whose every step is under the cap, which the session
accepts and writes.

Hermetic: no network, no LLM, no ``.env`` reliance — the scripted loop
replaces the only network-facing seam (:class:`AgentLoop`), while the
real prompt assembly, ``validate_plan_form_strict``, and the refine
path all run unmodified.
"""

from __future__ import annotations

import json
from pathlib import Path

from ferova.agent_engine.agent_loop import NimAgentOutput
from ferova.review.plan import PLAN_STEP_MAX_ACTION_DENSITY
from ferova.review.planner import Planner, run_planner_session

_SPEC_ID = "SP-TEST-DENSITY-GATE"


def _valid_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a minimal valid, strict-clean plan payload the session accepts."""
    return {
        "spec_id": spec_id,
        "title": "Density gate integration test",
        "summary": "Proves the action-density cap gates Planner emission end to end.",
        "steps": [
            {
                "index": 1,
                "title": "Add the demo module",
                "files": [
                    "src/ferova/density_gate_demo.py",
                    "tests/unit/test_density_gate_demo.py",
                    "tests/integration/test_density_gate_demo_flow.py",
                ],
                "action": "Create the demo module and wire it into the review package.",
                "commit_message": "feat(demo): add density gate demo module",
                "done_when": "pytest tests/unit/test_density_gate_demo.py is green",
                "unit_tests": ["tests/unit/test_density_gate_demo.py::test_happy_path"],
            }
        ],
        "integration_tests": ["tests/integration/test_density_gate_demo_flow.py"],
    }


def _dense_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a payload that passes pydantic validation but trips the density cap.

    Two files and an action longer than ``2 * PLAN_STEP_MAX_ACTION_DENSITY``
    chars yields a density well over the cap, while every other field
    stays otherwise valid (a promised unit test, a real commit message).
    """
    long_action = "Refactor the demo pipeline end to end: " + "x" * (
        2 * PLAN_STEP_MAX_ACTION_DENSITY + 100
    )
    return {
        "spec_id": spec_id,
        "title": "Density gate integration test",
        "summary": "Proves the action-density cap gates Planner emission end to end.",
        "steps": [
            {
                "index": 1,
                "title": "Add the dense demo module",
                "files": [
                    "tests/unit/test_density_gate_demo_a.py",
                    "tests/unit/test_density_gate_demo_b.py",
                ],
                "action": long_action,
                "commit_message": "feat(demo): add dense demo module",
                "done_when": "pytest tests/unit/test_density_gate_demo_a.py is green",
                "unit_tests": ["tests/unit/test_density_gate_demo_a.py::test_happy_path"],
            }
        ],
        "integration_tests": [],
    }


class _ScriptedLoop:
    """AgentLoop stand-in replaying a different output per ``run`` call.

    Mirrors the ``_ScriptedLoop`` pattern from
    ``tests/integration/test_planner_refine_history.py``: the
    exploration call (index 0, with tools) returns the first scripted
    text; every subsequent refinement call (no tools, indices 1+)
    returns the next scripted text. Every call's prompt, system, and
    tools are recorded for assertion.
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


def _seed_repo(tmp_path: Path) -> Path:
    """Seed a tmp repo with a throwaway governed spec doc the session can load."""
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / f"2026-07-09_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "ferova").mkdir(parents=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    return tmp_path


def test_density_cap_gates_planner_emission(tmp_path: Path) -> None:
    """A dense-step payload is refused and refined; the clean one is written."""
    repo = _seed_repo(tmp_path)

    dense = _dense_plan_payload()
    clean = _valid_plan_payload()

    texts = [
        f"```json\n{json.dumps(dense)}\n```",
        f"```json\n{json.dumps(clean)}\n```",
    ]
    loop = _ScriptedLoop(texts)
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert outcome.error is None
    assert len(loop.calls) == 2

    refine_prompt = str(loop.calls[1]["prompt"])
    assert str(PLAN_STEP_MAX_ACTION_DENSITY) in refine_prompt
    assert "30-turn" in refine_prompt
