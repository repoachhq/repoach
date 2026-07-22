"""Tests for the mechanical gap-refutation pass (SP-SELFVERIFY-REFUTABLE-GAPS).

Focus: :func:`repoach.review.devagent_selfverify._refute_gaps` and its wiring into
:func:`run_self_verify`. Drives the pass with real files under ``tmp_path`` and a
truthful boundary-fake judge reply (no monkeypatching of repoach functions), only
stubbing the ruff gate and branch diff exactly as the existing selfverify suite
does for its own boundary reasons.
"""

from __future__ import annotations

from pathlib import Path

from structlog.testing import capture_logs

import repoach.review.devagent_selfverify as sv
from repoach.review.devagent_selfverify import run_self_verify
from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec import SpecPlan

_AC_MD = (
    "# SP-T — demo\n\n## Goals\n\n- G1: do it\n\n"
    "## Acceptance Criteria\n\n- [ ] AC1: `tests/unit/test_x.py::test_ac1` passes.\n\n"
    "## Open Questions\n\n- none\n"
)


def _spec() -> SpecPlan:
    return SpecPlan(
        id="SP-T",
        file_path=Path("docs/specs/2026-07-22_SP-T_demo.md"),
        raw_markdown=_AC_MD,
        title="demo",
        summary="demo",
    )


def _plan() -> ActionPlan:
    return ActionPlan(
        spec_id="SP-T",
        title="demo",
        summary="demo",
        steps=[
            PlanStep(
                index=1,
                title="add x",
                files=["src/x.py", "tests/unit/test_x.py", "tests/integration/test_flow.py"],
                action="create x + test",
                commit_message="feat: x",
                done_when="green",
                unit_tests=["tests/unit/test_x.py::test_ac1"],
            )
        ],
        integration_tests=["tests/integration/test_flow.py"],
    )


def _repo_with_pattern(tmp_path: Path) -> Path:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text(
        "def test_ac1() -> None:\n    assert True\n", encoding="utf-8"
    )
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "chain_regen.py").write_text(
        "def gather_and_regenerate() -> None:\n"
        "    try:\n"
        "        sweep()\n"
        "    except Exception as exc:\n"
        "        log.warning('chain_regen_sweep_failed', error=str(exc))\n",
        encoding="utf-8",
    )
    return tmp_path


def _run(tmp_path: Path, judge_reply: str) -> sv.SelfVerifyResult:
    repo = _repo_with_pattern(tmp_path)
    import repoach.review.devagent_selfverify as sv_module

    original_ruff = sv_module.run_ruff_gate
    original_diff = sv_module._branch_diff
    sv_module.run_ruff_gate = lambda *a, **k: (True, "")
    sv_module._branch_diff = lambda *a, **k: "diff --git a/src/x.py b/src/x.py\n+x = 1"
    try:
        return run_self_verify(
            repo,
            spec=_spec(),
            plan=_plan(),
            suite_green=True,
            judge=lambda prompt: judge_reply,
        )
    finally:
        sv_module.run_ruff_gate = original_ruff
        sv_module._branch_diff = original_diff


def test_refuted_gap_is_dropped_and_logged(tmp_path: Path) -> None:
    reply = (
        '{"compliant": false, "reasons": "missing failure log", "gaps": ['
        '{"claim": "no chain_regen_sweep_failed log", '
        '"file": "src/chain_regen.py", '
        '"absent_pattern": "chain_regen_sweep_failed"}'
        "]}"
    )
    with capture_logs() as logs:
        result = _run(tmp_path, reply)

    events = [entry for entry in logs if entry["event"] == "selfverify.gap_refuted"]
    assert len(events) == 1
    assert events[0]["claim"] == "no chain_regen_sweep_failed log"
    assert events[0]["file"] == "src/chain_regen.py"
    assert events[0]["pattern"] == "chain_regen_sweep_failed"
    assert result.judge.gaps == []


def test_all_gaps_refuted_overturns_verdict(tmp_path: Path) -> None:
    reply = (
        '{"compliant": false, "reasons": "missing failure log", "gaps": ['
        '{"claim": "no chain_regen_sweep_failed log", '
        '"file": "src/chain_regen.py", '
        '"absent_pattern": "chain_regen_sweep_failed"}'
        "]}"
    )
    with capture_logs() as logs:
        result = _run(tmp_path, reply)

    events = {entry["event"] for entry in logs}
    assert "selfverify.gap_refuted" in events
    assert "selfverify.verdict_overturned_by_refutation" in events
    assert result.judge.compliant is True
    assert result.judge.gaps == []
    assert result.ok is True


def test_semantic_gap_survives_refutation(tmp_path: Path) -> None:
    reply = (
        '{"compliant": false, "reasons": "one real gap, one false one", "gaps": ['
        '{"claim": "no chain_regen_sweep_failed log", '
        '"file": "src/chain_regen.py", '
        '"absent_pattern": "chain_regen_sweep_failed"}, '
        '"the retry backoff logic is semantically wrong"'
        "]}"
    )
    with capture_logs() as logs:
        result = _run(tmp_path, reply)

    events = [entry for entry in logs if entry["event"] == "selfverify.gap_refuted"]
    assert len(events) == 1
    assert result.judge.compliant is False
    assert result.ok is False
    assert len(result.judge.gaps) == 1
    assert result.judge.gaps[0].claim == "the retry backoff logic is semantically wrong"
