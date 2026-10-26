"""SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS G5 — ``repoach spec audit --plans``."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.core.config as core_config
from repoach.cli.main import app
from repoach.review.plan import ActionPlan, PlanStep, render_plan_markdown


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REPOACH_DB_PATH", "REPOACH_FORGE"):
        monkeypatch.delenv(name, raising=False)
    core_config._settings = None


def _spec(spec_id: str, status: str, owns: str, criteria: str) -> str:
    return (
        "---\n"
        f"id: {spec_id}\n"
        f"title: {spec_id}\n"
        "version: 0.1\n"
        f"status: {status}\n"
        "owns:\n"
        f"  code: [{owns}]\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n"
        f"# {spec_id}\n\n"
        "## Acceptance Criteria\n\n"
        f"{criteria}\n"
        "## Open Questions\n\nNone.\n"
    )


def _plan(spec_id: str, files: list[str], selectors: list[str]) -> str:
    return render_plan_markdown(
        ActionPlan(
            spec_id=spec_id,
            title=spec_id,
            summary="Ship it.",
            steps=[
                PlanStep(
                    index=1,
                    title="Add it",
                    files=files,
                    action="Create it.",
                    commit_message="feat(x): add it",
                    done_when="green",
                    unit_tests=selectors,
                )
            ],
        )
    )


def _repo(tmp_path: Path, *, oversized_plan: bool = False) -> Path:
    root = tmp_path / "repo"
    specs = root / "docs/specs"
    plans = root / "docs/plans"
    specs.mkdir(parents=True)
    plans.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, capture_output=True)
    two = (
        "- [ ] AC1: `pytest -q tests/b.py::test_one` -> green.\n- [ ] AC2: `tests/b.py::test_two`\n"
    )
    prose = "- [ ] AC1: it works, said in prose.\n"
    (specs / "2026-10-26_SP-A_with-plan.md").write_text(
        _spec("SP-A", "approved", "src/z.py", prose), encoding="utf-8"
    )
    (specs / "2026-10-26_SP-B_without.md").write_text(
        _spec("SP-B", "approved", "src/a.py", two), encoding="utf-8"
    )
    (specs / "2026-10-26_SP-C_delivered.md").write_text(
        _spec("SP-C", "delivered", "", prose), encoding="utf-8"
    )
    (specs / "2026-10-26_SP-D_draft.md").write_text(
        _spec("SP-D", "draft", "src/d.py", prose), encoding="utf-8"
    )
    files = ["tests/t.py", "a.py", "b.py", "c.py"] if oversized_plan else ["tests/t.py"]
    selectors = [f"tests/t.py::test_{n}" for n in range(6 if oversized_plan else 1)]
    (plans / "SP-A.md").write_text(_plan("SP-A", files, selectors), encoding="utf-8")
    return root


def test_audit_plans_lists_specs_past_draft_without_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo(tmp_path))

    result = CliRunner().invoke(app, ["spec", "audit", "--plans"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == [
        "SP-B\tapproved\t2 selectors\tsrc/a.py",
        "SP-C\tdelivered\t0 selectors\t-",
        "2 specs past draft without a plan (1 with no selector to grade)",
    ]
    as_json = CliRunner().invoke(app, ["spec", "audit", "--plans", "--json"])
    assert as_json.exit_code == 0, as_json.output
    payload = json.loads(as_json.stdout)
    assert payload["specs"] == [
        {"id": "SP-B", "status": "approved", "selectors": 2, "owns_code": ["src/a.py"]},
        {"id": "SP-C", "status": "delivered", "selectors": 0, "owns_code": []},
    ]
    assert (payload["total"], payload["no_selectors"]) == (2, 1)


def test_audit_plans_lists_plan_steps_over_the_caps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo(tmp_path, oversized_plan=True))

    result = CliRunner().invoke(app, ["spec", "audit", "--plans"])

    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines()[-1] == "SP-A\tstep 1\t4/3 files\t6/5 selectors"
    payload = json.loads(CliRunner().invoke(app, ["spec", "audit", "--plans", "--json"]).stdout)
    assert payload["oversized_steps"] == [
        {
            "id": "SP-A",
            "step": 1,
            "files": 4,
            "files_cap": 3,
            "selectors": 6,
            "selectors_cap": 5,
        }
    ]


def test_audit_without_plans_flag_is_a_usage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(_repo(tmp_path))
    result = CliRunner().invoke(app, ["spec", "audit"])
    assert result.exit_code == 2


def test_audit_plans_tells_a_prose_plan_from_a_corrupt_action_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _repo(tmp_path)
    (root / "docs/plans/SP-B.md").write_text("# Plan\n\nProse only.\n", encoding="utf-8")
    (root / "docs/plans/SP-C.md").write_text(
        "# Plan\n\n<!-- repoach-action-plan -->\n```json\n{not json\n```\n", encoding="utf-8"
    )
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["spec", "audit", "--plans"])

    assert result.exit_code == 0, result.output
    lines = result.stdout.splitlines()
    assert lines[:2] == [
        "SP-B\tapproved\t2 selectors\tsrc/a.py",
        "1 specs past draft without a plan (0 with no selector to grade)",
    ]
    assert "SP-B\tnot an action plan: docs/plans/SP-B.md" in lines
    assert next(line for line in lines if "does not parse" in line).startswith(
        "SP-C\tplan does not parse: "
    )
