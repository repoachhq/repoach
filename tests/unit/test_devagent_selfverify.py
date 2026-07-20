"""Tests for SP-DEVAGENT-SELFVERIFY (slice 3): the self-verification gate.

Focus: the mechanical half (unit selectors present + suite green + ruff), the
semantic judge (compliant blocks / non-compliant blocks / unavailable fails open),
and the helpers (`_extract_acceptance_criteria`, `_parse_judge_verdict`). Ruff and
the branch diff are monkeypatched so the gate logic is exercised in isolation.
"""

from __future__ import annotations

from pathlib import Path

import repoach.review.devagent_selfverify as sv
from repoach.review.devagent_selfverify import (
    JudgeVerdict,
    _extract_acceptance_criteria,
    _parse_judge_verdict,
    run_self_verify,
)
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
        file_path=Path("docs/specs/2026-06-28_SP-T_demo.md"),
        raw_markdown=_AC_MD,
        title="demo",
        summary="demo",
    )


def _plan(unit_tests: list[str]) -> ActionPlan:
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
                unit_tests=unit_tests,
            )
        ],
        integration_tests=["tests/integration/test_flow.py"],
    )


def _repo_with_test(tmp_path: Path, symbol: str = "test_ac1") -> Path:
    (tmp_path / "tests" / "unit").mkdir(parents=True)
    (tmp_path / "tests" / "unit" / "test_x.py").write_text(
        f"def {symbol}() -> None:\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _compliant_judge(prompt: str) -> str:
    return '{"compliant": true, "reasons": "satisfies the spec", "gaps": []}'


def _noncompliant_judge(prompt: str) -> str:
    return '{"compliant": false, "reasons": "G1 not implemented", "gaps": ["G1 missing"]}'


def test_mechanical_and_judge_pass(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(
        sv, "_branch_diff", lambda *a, **k: "diff --git a/src/x.py b/src/x.py\n+x = 1"
    )

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=_compliant_judge,
    )

    assert result.ok is True
    assert result.mechanical_ok is True
    assert result.judge.available is True
    assert result.judge.compliant is True
    assert result.coverage.missing == ["tests/integration/test_flow.py"]


def test_missing_unit_selector_blocks(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path, symbol="test_ac1")
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_absent"]),
        suite_green=True,
        judge=_compliant_judge,
    )

    assert result.ok is False
    assert result.mechanical_ok is False
    assert any("absent" in r for r in result.reasons)


def test_ruff_red_blocks(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (False, "E501 line too long"))

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=_compliant_judge,
    )

    assert result.ok is False
    assert result.mechanical_ok is False
    assert any("ruff" in r for r in result.reasons)
    assert result.judge.available is False
    assert "skipped" in result.judge.reasons


def test_suite_red_blocks(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=False,
        judge=_compliant_judge,
    )

    assert result.ok is False
    assert result.mechanical_ok is False


def test_judge_noncompliant_blocks(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sv, "_branch_diff", lambda *a, **k: "some diff")

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=_noncompliant_judge,
    )

    assert result.mechanical_ok is True
    assert result.judge.available is True
    assert result.judge.compliant is False
    assert result.ok is False
    assert any("not compliant" in r for r in result.reasons)


def test_judge_raises_fails_open(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sv, "_branch_diff", lambda *a, **k: "some diff")

    def _boom(prompt: str) -> str:
        raise RuntimeError("proxy down")

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=_boom,
    )

    assert result.mechanical_ok is True
    assert result.judge.available is False
    assert result.ok is True


def test_judge_unparseable_fails_open(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sv, "_branch_diff", lambda *a, **k: "some diff")

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=lambda p: "no json here, sorry",
    )

    assert result.judge.available is False
    assert result.ok is True


def test_empty_diff_makes_judge_unavailable(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(sv, "_branch_diff", lambda *a, **k: "")

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=_noncompliant_judge,
    )

    assert result.judge.available is False
    assert result.ok is True


def test_judge_none_fails_open(tmp_path: Path, monkeypatch) -> None:
    repo = _repo_with_test(tmp_path)
    monkeypatch.setattr(sv, "run_ruff_gate", lambda *a, **k: (True, ""))

    result = run_self_verify(
        repo,
        spec=_spec(),
        plan=_plan(["tests/unit/test_x.py::test_ac1"]),
        suite_green=True,
        judge=None,
    )

    assert result.mechanical_ok is True
    assert result.judge.available is False
    assert result.ok is True


def test_extract_acceptance_criteria() -> None:
    section = _extract_acceptance_criteria(_AC_MD)
    assert "AC1" in section
    assert "Open Questions" not in section
    assert _extract_acceptance_criteria("# no AC section here\n") == ""
    eof_md = "# T\n\n## Acceptance Criteria\n\n- [ ] AC1: final section at EOF\n"
    assert "AC1" in _extract_acceptance_criteria(eof_md)


def test_parse_judge_verdict() -> None:
    ok = _parse_judge_verdict('prose {"compliant": true, "reasons": "r", "gaps": []} trailing')
    assert ok is not None
    assert ok.available is True
    assert ok.compliant is True

    bad = _parse_judge_verdict('{"compliant": false, "reasons": "r", "gaps": ["a", "b"]}')
    assert bad is not None
    assert bad.compliant is False
    assert bad.gaps == ["a", "b"]

    assert _parse_judge_verdict("not json") is None
    assert _parse_judge_verdict('{"reasons": "no compliant key"}') is None
    assert _parse_judge_verdict('{"compliant": "true"}') is None

    coerced = _parse_judge_verdict('{"compliant": false, "gaps": "oops not a list"}')
    assert coerced is not None
    assert coerced.gaps == []

    multi = _parse_judge_verdict(
        'example: {"compliant": true, "reasons": "x", "gaps": []}\n'
        'verdict: {"compliant": false, "reasons": "real", "gaps": ["G1"]}'
    )
    assert multi is not None
    assert multi.compliant is False
    assert multi.gaps == ["G1"]


def test_judge_verdict_defaults_fail_open() -> None:
    v = JudgeVerdict()
    assert v.available is False
    assert v.compliant is False
