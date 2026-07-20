"""SP-PLANNER-REFINE-HISTORY integration — two-error session converges with history.

Drives ``run_planner_session`` end-to-end with a scripted fake
AgentLoop that replays a different candidate on each call:

* attempt 1 — missing ``commit_message`` (validation error)
* attempt 2 — wrong ``spec_id`` (mismatch error)
* attempt 3 — valid plan (accepted)

Asserts that the session succeeds, that the third prompt (the refine
call that produced the accepted plan) contains BOTH prior errors
numbered oldest first, and that the ``planner.plan_invalid`` log lines
carry ``errors_so_far`` counts of 1 then 2.

A sibling test sets ``REPOACH_PLANNER_PARSE_ATTEMPTS=2`` with a
never-valid candidate and asserts exactly 2 attempts with the
exhausted-session error naming both failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from repoach.agent_engine.agent_loop import NimAgentOutput
from repoach.review.planner import Planner, run_planner_session

_SPEC_ID = "SP-TEST-REFINE-HIST-INT"

_ERROR_1_TEXT = "plan payload failed validation"
_ERROR_2_TEXT = "requested"


def _valid_plan_payload(spec_id: str = _SPEC_ID) -> dict:
    """Return a minimal valid plan payload the session will accept."""
    return {
        "spec_id": spec_id,
        "title": "Refine history integration test",
        "summary": "Exercises the full error-history refine loop end to end.",
        "steps": [
            {
                "index": 1,
                "title": "Add the demo module",
                "files": [
                    "src/repoach/refine_demo.py",
                    "tests/unit/test_refine_demo.py",
                    "tests/integration/test_refine_demo_flow.py",
                ],
                "action": "Create the demo module and wire it into the review package.",
                "commit_message": "feat(refine): add refine history demo module",
                "done_when": "pytest tests/unit/test_refine_demo.py is green",
                "unit_tests": ["tests/unit/test_refine_demo.py::test_happy_path_converges"],
            }
        ],
        "integration_tests": ["tests/integration/test_refine_demo_flow.py"],
    }


class _ScriptedLoop:
    """AgentLoop stand-in replaying a different output per ``run`` call.

    The exploration call (index 0, with tools) always returns the
    first (invalid) candidate.  Every subsequent refinement call
    (no tools, indices 1+) returns the next scripted text.
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
    """Seed a tmp repo with a spec doc the session can load."""
    specs = tmp_path / "docs" / "specs"
    specs.mkdir(parents=True)
    (specs / f"2026-07-06_{_SPEC_ID}_demo.md").write_text(
        f"# {_SPEC_ID} — demo spec\n\n## Why\n\nBecause tests.\n\n"
        "## Definition of Done\n\n- it works\n",
        encoding="utf-8",
    )
    (tmp_path / "src" / "repoach").mkdir(parents=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "integration").mkdir(parents=True)
    return tmp_path


def test_two_error_session_converges_with_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-error session converges and the third prompt carries full history."""
    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "5")
    repo = _seed_repo(tmp_path)

    valid = _valid_plan_payload()

    bad_a = dict(_valid_plan_payload())
    del bad_a["steps"][0]["commit_message"]

    bad_b = dict(_valid_plan_payload(), spec_id="SP-WRONG")

    texts = [
        f"```json\n{json.dumps(bad_a)}\n```",
        f"```json\n{json.dumps(bad_b)}\n```",
        f"```json\n{json.dumps(valid)}\n```",
    ]
    loop = _ScriptedLoop(texts)
    planner = Planner(loop=loop, repo_root=repo)

    with capture_logs() as logs:
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is True
    assert outcome.error is None

    assert len(loop.calls) == 3

    refine_prompt = str(loop.calls[2]["prompt"])
    assert "1." in refine_prompt
    assert "2." in refine_prompt
    assert "REJECTED" in refine_prompt
    assert _ERROR_1_TEXT in refine_prompt
    assert _ERROR_2_TEXT in refine_prompt
    assert refine_prompt.index("1.") < refine_prompt.index("2.")

    invalid_logs = [entry for entry in logs if entry.get("event") == "planner.plan_invalid"]
    assert len(invalid_logs) == 2
    assert invalid_logs[0].get("errors_so_far") == 1
    assert invalid_logs[1].get("errors_so_far") == 2


def test_exhausted_session_with_two_attempts_names_both_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """REPOACH_PLANNER_PARSE_ATTEMPTS=2 → exactly 2 attempts, error names both."""
    monkeypatch.setenv("REPOACH_PLANNER_PARSE_ATTEMPTS", "2")
    repo = _seed_repo(tmp_path)

    bad_a = dict(_valid_plan_payload())
    del bad_a["steps"][0]["commit_message"]

    bad_b = dict(_valid_plan_payload(), spec_id="SP-WRONG")

    texts = [
        f"```json\n{json.dumps(bad_a)}\n```",
        f"```json\n{json.dumps(bad_b)}\n```",
    ]
    loop = _ScriptedLoop(texts)
    planner = Planner(loop=loop, repo_root=repo)

    with capture_logs() as logs:
        outcome = run_planner_session(_SPEC_ID, root=repo, planner=planner)

    assert outcome.written is False
    assert outcome.error is not None
    assert len(loop.calls) == 2

    error_text = outcome.error
    assert "attempt 1" in error_text
    assert "attempt 2" in error_text
    assert _ERROR_1_TEXT in error_text
    assert _ERROR_2_TEXT in error_text

    invalid_logs = [entry for entry in logs if entry.get("event") == "planner.plan_invalid"]
    assert len(invalid_logs) == 2
    assert invalid_logs[0].get("errors_so_far") == 1
    assert invalid_logs[1].get("errors_so_far") == 2
