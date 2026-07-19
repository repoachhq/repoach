"""SP-PLANNER-SELECTOR-CHECK integration — end-to-end selector check via run_planner_session.

Complements the unit-level ``TestSelectorCheck`` coverage in
``tests/unit/test_review_planner.py`` by driving the FULL
``run_planner_session`` entry point (spec load, Planner.plan, plan
document write) with a fake ``AgentLoop`` — the path a real build
actually travels — rather than calling ``Planner.plan`` directly.

A plan whose step promises a nonexistent node id in an EXISTING test
file must be rejected end to end: nothing written under
``docs/plans/``, and the loud ``PlannerOutcome.error`` names both the
offending selector and the two remedies
(:func:`repoach.review.spec_gate.selector_present` resolving it, or
declaring creation verbatim in the step's action text). The same plan
with the node id declared in the action text must be accepted.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoach.agent_engine.agent_loop import NimAgentOutput
from repoach.review.planner import Planner, run_planner_session

_SPEC_ID = "SP-TEST-SELECTOR-CHECK-INT"

_GHOST_SELECTOR = "tests/unit/test_review_planner.py::test_ghost_symbol"


class _FakeLoop:
    """AgentLoop stand-in replaying the same canned output on every call.

    A rejected plan drives the retry loop through its full
    ``_PLAN_PARSE_ATTEMPTS`` budget; replaying the identical output
    keeps every attempt equally invalid so the session gives up loudly
    with the same selector directive, exactly like a model that never
    corrects the hallucinated node id.
    """

    def __init__(self, output: NimAgentOutput) -> None:
        self.output = output
        self.calls: list[dict[str, object]] = []

    def run(self, prompt: str, *, system: str | None = None, tools: list | None = None):
        self.calls.append({"prompt": prompt, "system": system or "", "tools": tools or []})
        return self.output


def _repo_with_spec(tmp_path: Path) -> Path:
    """Seed a tmp repo with a spec doc and an existing unit-test file.

    The existing file lives at ``tests/unit/test_review_planner.py``
    (the step brief's example path) and defines one real test symbol
    so ``selector_present`` genuinely resolves it, but never defines
    ``test_ghost_symbol`` — that node id is a hallucination.
    """
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / f"2026-07-06_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    unit_dir = tmp_path / "tests" / "unit"
    unit_dir.mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    (unit_dir / "test_review_planner.py").write_text(
        "def test_real_symbol() -> None:\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _plan_payload(*, declare_ghost_in_action: bool) -> dict:
    """Return a plan payload promising ``_GHOST_SELECTOR``.

    Args:
        declare_ghost_in_action: When true, the step's action text
            names ``test_ghost_symbol`` verbatim (declared creation,
            the second legal remedy); when false, the action text
            says nothing about it, leaving the promise a bare
            hallucination.
    """
    action = (
        "Create the demo module and add test_ghost_symbol to test_review_planner.py to cover it."
        if declare_ghost_in_action
        else "Create the demo module and wire it into the review package."
    )
    return {
        "spec_id": _SPEC_ID,
        "title": "Selector check end-to-end demo",
        "summary": "Exercise the mechanical selector check through run_planner_session.",
        "steps": [
            {
                "index": 1,
                "title": "Add the demo module",
                "files": [
                    "src/repoach/demo_selector_check.py",
                    "tests/unit/test_review_planner.py",
                    "tests/integration/test_demo_selector_check_flow.py",
                ],
                "action": action,
                "commit_message": "feat(demo): add selector check demo module",
                "done_when": "pytest tests/unit/test_review_planner.py is green",
                "unit_tests": [_GHOST_SELECTOR],
            }
        ],
        "integration_tests": ["tests/integration/test_demo_selector_check_flow.py"],
    }


def _loop_with(text: str) -> _FakeLoop:
    return _FakeLoop(
        NimAgentOutput(
            text=text,
            tool_calls_made=["list_dir", "read_file"],
            elapsed_s=1.0,
            tokens_used=100,
            model_used="fake/coder",
            turns=1,
        )
    )


def test_planner_session_rejects_hallucinated_selector_in_existing_file(tmp_path: Path) -> None:
    repo = _repo_with_spec(tmp_path)
    rejected_text = f"```json\n{json.dumps(_plan_payload(declare_ghost_in_action=False))}\n```"
    loop = _loop_with(rejected_text)
    planner = Planner(loop=loop, repo_root=repo)

    outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is False
    assert outcome.error is not None
    assert _GHOST_SELECTOR in outcome.error
    assert "selector_present" in outcome.error
    assert "node id" in outcome.error
    assert not (repo / "docs" / "plans").exists()

    accepted_text = f"```json\n{json.dumps(_plan_payload(declare_ghost_in_action=True))}\n```"
    accepted_loop = _loop_with(accepted_text)
    accepted_planner = Planner(loop=accepted_loop, repo_root=repo)

    accepted_outcome = run_planner_session(_SPEC_ID, root=repo, planner=accepted_planner)

    assert accepted_outcome.written is True
    assert accepted_outcome.error is None
    assert accepted_outcome.plan_path == f"docs/plans/{_SPEC_ID}.md"
    assert (repo / accepted_outcome.plan_path).is_file()
