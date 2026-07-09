"""SP-PLAN-QUALITY step 6 — end-to-end proof the rule catalog arrives first.

Drives ``run_planner_session`` end to end against a throwaway spec in
a ``tmp_path`` repo with a scripted :class:`AgentLoop` stand-in that
immediately answers a valid, strict-clean plan payload — no failure
ever occurs in this session. The whole point of SP-PLAN-QUALITY is
that the full rule catalog rides the FIRST request the Planner ever
sends, not just the refine turns after a rejection
(``tests/integration/test_planner_refine_history.py`` and
``tests/unit/test_planner_prompt_rules.py`` already cover the refine
path); this test closes the loop by proving the catalog is present
before any failure has a chance to happen, and that the session still
writes the plan.

Hermetic: no network, no LLM, no ``.env`` reliance — the scripted loop
replaces the only network-facing seam (:class:`AgentLoop`), while the
real prompt assembly, pydantic validation, strict production-time
layer, and telemetry recording all run unmodified.
"""

from __future__ import annotations

import json
from pathlib import Path

from ferova.agent_engine.agent_loop import NimAgentOutput
from ferova.review.plan import render_plan_form_rules
from ferova.review.planner import Planner, run_planner_session

_SPEC_ID = "SP-TEST-FORM-CONVERGENCE"
_HEADING = "Plan-form rules (all of them — every attempt is validated against every rule)"


def _valid_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a minimal valid, strict-clean plan payload the session accepts."""
    return {
        "spec_id": spec_id,
        "title": "Form convergence integration test",
        "summary": "Proves the full rule catalog rides the very first Planner request.",
        "steps": [
            {
                "index": 1,
                "title": "Add the demo module",
                "files": [
                    "src/ferova/form_convergence_demo.py",
                    "tests/unit/test_form_convergence_demo.py",
                    "tests/integration/test_form_convergence_demo_flow.py",
                ],
                "action": "Create the demo module and wire it into the review package.",
                "commit_message": "feat(demo): add form convergence demo module",
                "done_when": "pytest tests/unit/test_form_convergence_demo.py is green",
                "unit_tests": ["tests/unit/test_form_convergence_demo.py::test_happy_path"],
            }
        ],
        "integration_tests": ["tests/integration/test_form_convergence_demo_flow.py"],
    }


class _ScriptedLoop:
    """AgentLoop stand-in that always answers a single scripted candidate.

    Mirrors the ``_ScriptedLoop`` pattern from
    ``tests/integration/test_planner_refine_history.py``: every call's
    prompt, system, and tools are recorded so the test can inspect the
    very first request the session ever sent.
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
    (specs / f"2026-07-08_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "ferova").mkdir(parents=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    return tmp_path


def test_catalog_present_in_first_planner_request(tmp_path: Path) -> None:
    """The full rule catalog rides the FIRST request, before any failure occurs."""
    repo = _seed_repo(tmp_path)

    valid_text = f"```json\n{json.dumps(_valid_plan_payload())}\n```"
    loop = _ScriptedLoop([valid_text])
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert outcome.error is None
    assert len(loop.calls) == 1

    first_system = str(loop.calls[0]["system"])
    assert _HEADING in first_system

    catalog = render_plan_form_rules()
    sentences = [line.split(". ", 1)[1] for line in catalog.splitlines() if line.strip()]
    assert len(sentences) >= 3
    for sentence in sentences[:3]:
        assert sentence in first_system
