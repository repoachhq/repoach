"""End-to-end integration test for the self-verify refutation pass.

SP-SELFVERIFY-REFUTABLE-GAPS, AC4: drives the real :func:`run_self_verify`
against a real tmp repo whose file CONTAINS the pattern a judge falsely claims
is absent. The judge is a truthful boundary-fake (the ``judge=`` callable is
the external-LLM boundary :func:`run_self_verify` is designed to take a fake
for, exactly as ``tests/unit/test_devagent_selfverify.py`` does) that replies
non-compliant with one evidence-bearing gap. Only ``run_ruff_gate`` and
``_branch_diff`` are stubbed, matching that same suite's boundary handling
(ruff and git are process-spawning externals, not the module under test).

The mechanical refutation pass must see the pattern genuinely present at
HEAD, drop the gap, overturn the verdict, and the caller must observe both
``selfverify.gap_refuted`` and ``selfverify.verdict_overturned_by_refutation``
audit events via ``structlog.testing.capture_logs``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import structlog
from structlog.testing import capture_logs

import repoach.review.devagent_selfverify as sv
from repoach.review.devagent_selfverify import run_self_verify
from repoach.review.plan import ActionPlan, PlanStep
from repoach.review.spec import SpecPlan


@pytest.fixture(autouse=True)
def _fresh_selfverify_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebind the module logger so ``capture_logs`` sees its events.

    ``configure_logging`` (exercised by earlier suites in serial order)
    sets ``cache_logger_on_first_use=True``; a proxy cached before this
    test keeps its materialized processor chain and bypasses the
    ``capture_logs`` swap. A fresh lazy proxy binds inside the capture
    context instead.
    """
    monkeypatch.setattr(sv, "_log", structlog.get_logger("selfverify.refutation.test"))


_AC_MD = (
    "# SP-T — demo\n\n## Goals\n\n- G1: log the sweep failure\n\n"
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


def _repo_with_pattern_present(tmp_path: Path) -> Path:
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


def _false_absence_judge(prompt: str) -> str:
    del prompt
    return (
        '{"compliant": false, "reasons": "no failure log on the sweep", "gaps": ['
        '{"claim": "gather_and_regenerate emits no chain_regen_sweep_failed log", '
        '"file": "src/chain_regen.py", '
        '"absent_pattern": "chain_regen_sweep_failed"}'
        "]}"
    )


def test_false_absence_verdict_overturned_end_to_end(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_pattern_present(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        sv, "_branch_diff", lambda *a, **k: "diff --git a/src/x.py b/src/x.py\n+x = 1"
    )

    with capture_logs() as logs:
        result = run_self_verify(
            repo,
            spec=_spec(),
            plan=_plan(),
            suite_green=True,
            judge=_false_absence_judge,
        )

    assert result.ok is True
    assert result.mechanical_ok is True
    assert result.judge.available is True
    assert result.judge.compliant is True
    assert result.judge.gaps == []

    events = {entry["event"] for entry in logs}
    assert "selfverify.gap_refuted" in events
    assert "selfverify.verdict_overturned_by_refutation" in events

    refuted = next(entry for entry in logs if entry["event"] == "selfverify.gap_refuted")
    assert refuted["file"] == "src/chain_regen.py"
    assert refuted["pattern"] == "chain_regen_sweep_failed"
