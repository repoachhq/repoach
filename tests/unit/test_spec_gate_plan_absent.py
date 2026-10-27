"""SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS — a spec without a plan is graded on its ACs.

A spec past draft with no ``docs/plans/<id>.md`` used to be refused with
the unnamed sentence ``spec acceptance selectors not all present``. It is
now graded against the selectors of its Acceptance Criteria, the union of
base and head, and every refusal names the selector and the criterion
or plan step that promised it.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from typer.testing import CliRunner

import repoach.core.config as core_config
from repoach.cli.review_cmds import review_app
from repoach.forge import Diff, ForgeWriteBarrier, PullRequest
from repoach.gates.config import DEFAULT_REFUTER_BUDGET
from repoach.review.auto_merge import CIGateOutcome, GateEvaluation, decide_at_head
from repoach.review.findings import init_findings_schema, record_review_integrity
from repoach.review.merge_gate import compute_merge_decision, summarise_ledger_facts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.plan import ActionPlan, PlanStep, render_plan_markdown
from repoach.review.spec_gate import (
    SPEC_COVERAGE_RULE_VERSION,
    SpecCoverage,
    coverage_at_head,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    latest_batch,
    record_spec_coverage,
    record_spec_coverages,
    spec_acceptance_criteria,
)
from repoach.review.spec_grading import coverage_rows_at_head

_ID = "SP-PLANLESS"
_SPEC_PATH = f"docs/specs/2026-10-26_{_ID}_planless.md"
_CODE = "src/planless/core.py"
_PRESENT = ["tests/unit/test_planless.py::test_one", "tests/unit/test_planless.py::test_two"]
_ABSENT = "tests/x.py::test_y"
_PLAN_SELECTOR = "tests/p.py::test_p"
_HEADER = "spec acceptance selectors not all present"
_CORRUPT_PLAN = "# Plan\n\n<!-- repoach-action-plan -->\n```json\n{not json\n```\n"


@pytest.fixture(autouse=True)
def _clean_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("REPOACH_DB_PATH", "REPOACH_FORGE", "REPOACH_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    core_config._settings = None


def _spec(status: str, criteria: list[str]) -> str:
    lines = "".join(f"- [ ] AC{n}: {text}\n" for n, text in enumerate(criteria, start=1))
    return (
        "---\n"
        f"id: {_ID}\n"
        "title: Planless\n"
        "version: 0.1\n"
        f"status: {status}\n"
        "owns:\n"
        f"  code: [{_CODE}]\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n"
        "# Planless\n\n"
        "## Acceptance Criteria\n\n"
        f"{lines}\n"
        "## Open Questions\n\nNone.\n"
    )


def _criteria(*selectors: str) -> list[str]:
    return [f"`pytest -q {selector}` -> green." for selector in selectors]


def _plan(selector: str) -> str:
    return render_plan_markdown(
        ActionPlan(
            spec_id=_ID,
            title="Planless",
            summary="Ship it.",
            steps=[
                PlanStep(
                    index=1,
                    title="Add it",
                    files=["tests/p.py"],
                    action="Create the test.",
                    commit_message="test(gate): add it",
                    done_when="green",
                    unit_tests=[selector],
                )
            ],
        )
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _write(repo: Path, files: dict[str, str]) -> None:
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _tests_file(*names: str) -> str:
    return "".join(f"def {name}():\n    assert 1 + 1 == 2\n\n\n" for name in names)


def _repo(
    tmp_path: Path,
    *,
    base: dict[str, str],
    head: dict[str, str],
    remove: tuple[str, ...] = (),
) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    _write(repo, {"README.md": "base\n", **base})
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "feat/planless")
    _write(repo, head)
    for path in remove:
        (repo / path).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    return repo, _git(repo, "rev-parse", "HEAD")


def _present_tests() -> dict[str, str]:
    return {"tests/unit/test_planless.py": _tests_file("test_one", "test_two")}


class _Forge:
    """Serves the PR's base ref and diff; refuses fetches."""

    def __init__(self, changed: list[str]) -> None:
        self.barrier = ForgeWriteBarrier()
        self.changed = changed

    def pull_request(self, pr_number: int) -> SimpleNamespace:
        return SimpleNamespace(base_ref="develop")

    def diff(self, pr_number: int) -> SimpleNamespace:
        return SimpleNamespace(
            text="".join(f"diff --git a/{p} b/{p}\n--- a/{p}\n+++ b/{p}\n" for p in self.changed)
        )

    def git(self, args: list[str]) -> str:
        raise AssertionError(f"the base ref resolves locally, no fetch expected: {args}")


def _ledger(tmp_path: Path, head: str) -> Path:
    db = tmp_path / "state" / "review.db"
    init_findings_schema(db)
    init_spec_coverage_schema(db)
    record_review_integrity(db, pr_number=7, head_sha=head, n_reviewers=4, n_unparsed=0)
    return db


def _graded(repo: Path, tmp_path: Path, head: str, changed: list[str]) -> Path:
    db = _ledger(tmp_path, head)
    grading = coverage_rows_at_head(repo, spec_id=_ID, base_ref="develop", changed_files=changed)
    record_spec_coverages(db, pr_number=7, head_sha=head, coverages=grading.rows)
    return db


def _decide(repo: Path, db: Path, head: str, changed: list[str]) -> tuple[object, object]:
    _, facts, decision = decide_at_head(
        7,
        forge=_Forge(changed),
        db=db,
        ci_green=True,
        repo_root=repo,
        head_sha=head,
        max_judged=0,
        head_tree=True,
        budget=DEFAULT_REFUTER_BUDGET,
        base_ref="develop",
    )
    return facts, decision


def test_approved_spec_without_plan_merges_when_its_ac_selectors_are_present(
    tmp_path: Path,
) -> None:
    spec = _spec("approved", _criteria(*_PRESENT))
    repo, head = _repo(
        tmp_path,
        base={_SPEC_PATH: spec},
        head={_CODE: "VALUE = 1\n", **_present_tests()},
    )
    changed = [_CODE, "tests/unit/test_planless.py"]
    db = _graded(repo, tmp_path, head, changed)

    _, decision = _decide(repo, db, head, changed)

    row = fetch_spec_coverage(db, 7, head_sha=head)[-1]
    assert decision.merge is True, decision.reasons
    assert row.contract == "acceptance-criteria"
    assert row.missing == []
    assert row.n_promised == 2


def test_approved_spec_without_plan_names_the_absent_selector_and_its_ac(
    tmp_path: Path,
) -> None:
    spec = _spec("approved", _criteria(*_PRESENT, _ABSENT))
    repo, head = _repo(
        tmp_path,
        base={_SPEC_PATH: spec},
        head={_CODE: "VALUE = 1\n", **_present_tests()},
    )
    changed = [_CODE, "tests/unit/test_planless.py"]
    db = _graded(repo, tmp_path, head, changed)

    _, decision = _decide(repo, db, head, changed)

    assert decision.merge is False
    assert decision.reasons == [f"{_HEADER}: {_ABSENT} (AC3)"]
    assert not any("plan-absent" in reason for reason in decision.reasons)


def test_plan_wins_over_the_ac_section(tmp_path: Path) -> None:
    spec = _spec("approved", _criteria(_ABSENT))
    plan_path = f"docs/plans/{_ID}.md"
    repo, head = _repo(
        tmp_path,
        base={_SPEC_PATH: spec, plan_path: _plan(_PLAN_SELECTOR)},
        head={_CODE: "VALUE = 1\n", "tests/p.py": _tests_file("test_p")},
    )
    changed = [_CODE, "tests/p.py"]
    db = _graded(repo, tmp_path, head, changed)

    _, decision = _decide(repo, db, head, changed)

    row = fetch_spec_coverage(db, 7, head_sha=head)[-1]
    assert decision.merge is True, decision.reasons
    assert row.contract == "plan"
    assert row.report == []

    (repo / "tests/p.py").unlink()
    coverage, _ = coverage_at_head(repo, spec_id=_ID, base_ref="develop", changed_files=changed)
    assert coverage.contract == "plan"
    db2 = _ledger(tmp_path / "second", head)
    record_spec_coverage(db2, pr_number=7, head_sha=head, coverage=coverage)
    reasons = compute_merge_decision(summarise_ledger_facts(db2, pr_number=7, head_sha=head))
    assert reasons.reasons == [f"{_HEADER}: {_PLAN_SELECTOR} (plan step 1)"]


def _no_selector_line(status: str) -> str:
    return (
        f"no selectors to grade: {_ID} ({status}) has no docs/plans/{_ID}.md "
        "and its Acceptance Criteria name no pytest selector"
    )


def _gate_json(
    repo: Path, monkeypatch: pytest.MonkeyPatch, facts: object, decision: object, head: str
) -> tuple[dict[str, object], str]:
    monkeypatch.chdir(repo)
    evaluation = GateEvaluation(
        head_sha=head,
        ci_gate=CIGateOutcome(green=True, outcome_tag="", reason=""),
        facts=facts,
        decision=decision,
        pull_request=PullRequest(
            number=7, head_ref="feat/planless", base_ref="develop", body=f"Spec: {_ID}\n"
        ),
        changed_files=[],
    )
    with patch("repoach.cli.review_cmds.evaluate_merge_gate", return_value=evaluation):
        result = CliRunner().invoke(review_app, ["gate", "7"])
    return json.loads(result.stdout), result.stderr


@pytest.mark.parametrize("status", ["approved", "draft"])
def test_zero_selector_contract_is_named_and_refuses_only_code_prs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, status: str
) -> None:
    spec = _spec(status, ["the gate says so, in prose only."])
    repo, head = _repo(
        tmp_path, base={_SPEC_PATH: spec}, head={_CODE: "VALUE = 1\n", "docs/notes.md": "n\n"}
    )
    line = _no_selector_line(status)

    db = _graded(repo, tmp_path, head, [_CODE])
    _, decision = _decide(repo, db, head, [_CODE])
    assert decision.merge is False
    assert line in decision.reasons

    docs_db = _graded(repo, tmp_path / "docs-only", head, ["docs/notes.md"])
    facts, decision = _decide(repo, docs_db, head, ["docs/notes.md"])
    assert decision.merge is True, decision.reasons
    payload, _ = _gate_json(repo, monkeypatch, facts, decision, head)
    acceptance = payload["spec_acceptance"]
    assert acceptance["contract"] == "none"
    assert acceptance["report"] == [line]


def _orchestrated_team(repo: Path, head: str, changed: list[str]) -> object:
    forge = MagicMock()
    forge.diff.return_value = Diff(
        text="".join(f"diff --git a/{p} b/{p}\n" for p in changed), files=changed
    )
    forge.pull_request.return_value = PullRequest(
        number=7, head_ref="feat/planless", base_ref="develop", body=f"Spec: {_ID}\n"
    )
    forge.head_sha.return_value = head
    forge.comments.return_value = []
    forge.barrier = ForgeWriteBarrier()
    orch = ReviewTeamOrchestrator(
        forge=forge, db_path=repo.parent / "orch.db", post_to_github=False, repo_root=repo
    )
    outcome = MagicMock(
        role=MagicMock(value="x"),
        verdict=MagicMock(value="APPROVE"),
        summary="ok",
        comments=[],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="",
    )
    with (
        patch("repoach.review.reviewer.AgentLoop", return_value=MagicMock()),
        patch("repoach.review.reviewer.Architect.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Sentinel.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Tester.review_diff", return_value=outcome),
        patch("repoach.review.reviewer.Scribe.review_diff", return_value=outcome),
    ):
        return orch.review_pr(7)


def test_plan_absent_report_line_shape(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec("approved", _criteria(*_PRESENT))
    repo, head = _repo(
        tmp_path, base={_SPEC_PATH: spec}, head={_CODE: "VALUE = 1\n", **_present_tests()}
    )
    changed = [_CODE, "tests/unit/test_planless.py"]
    line = f"plan-absent: graded against the acceptance criteria of {_ID} (2 selectors)"

    team = _orchestrated_team(repo, head, changed)
    monkeypatch.chdir(repo)
    with patch("repoach.cli.review_cmds.run_review", return_value=team):
        review = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])
    assert review.stderr.splitlines().count(line) == 1, review.stderr
    assert sum("plan-absent" in out for out in review.stderr.splitlines()) == 1

    db = _graded(repo, tmp_path, head, changed)
    facts, decision = _decide(repo, db, head, changed)
    payload, stderr = _gate_json(repo, monkeypatch, facts, decision, head)
    assert stderr.splitlines().count(line) == 1, stderr
    assert sum("plan-absent" in out for out in stderr.splitlines()) == 1
    assert payload["spec_acceptance"]["contract"] == "acceptance-criteria"
    assert payload["spec_acceptance"]["report"] == [line]


def test_draft_to_approved_on_the_pr_grades_the_union_of_base_and_head_acs(
    tmp_path: Path,
) -> None:
    first = "tests/unit/test_union.py::test_a"
    second = "tests/unit/test_union.py::test_b"
    repo, _ = _repo(
        tmp_path,
        base={_SPEC_PATH: _spec("draft", _criteria(first))},
        head={_SPEC_PATH: _spec("approved", _criteria(first, second))},
    )

    coverage, _ = coverage_at_head(
        repo, spec_id=_ID, base_ref="develop", changed_files=[_SPEC_PATH]
    )
    assert coverage.contract == "acceptance-criteria"
    assert coverage.missing == [first, second]
    assert coverage.planned == []
    assert coverage.covered is False

    _write(repo, {_SPEC_PATH: _spec("approved", _criteria(second))})
    _git(repo, "commit", "-q", "-am", "drop the base selector from the head AC")
    coverage, _ = coverage_at_head(
        repo, spec_id=_ID, base_ref="develop", changed_files=[_SPEC_PATH]
    )
    assert coverage.missing == [first, second]
    assert coverage.origins == {first: "AC1", second: "AC1"}


def test_base_ref_unavailable_and_unparseable_plan_have_their_own_sentences(
    tmp_path: Path,
) -> None:
    spec = _spec("approved", _criteria(_ABSENT))
    repo, head = _repo(
        tmp_path,
        base={_SPEC_PATH: spec, f"docs/plans/{_ID}.md": _CORRUPT_PLAN},
        head={_CODE: "VALUE = 1\n"},
    )

    unavailable, _ = coverage_at_head(
        repo, spec_id=_ID, base_ref="no-such-branch", changed_files=[_CODE]
    )
    db = _ledger(tmp_path, head)
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=unavailable)
    reasons = compute_merge_decision(summarise_ledger_facts(db, pr_number=7, head_sha=head))
    assert unavailable.contract == "unavailable"
    assert reasons.reasons == [
        "spec coverage not graded: base ref no-such-branch unavailable, "
        "the contract cannot be read (fail closed)"
    ]

    unparseable, _ = coverage_at_head(repo, spec_id=_ID, base_ref="develop", changed_files=[_CODE])
    db2 = _ledger(tmp_path / "plan", head)
    record_spec_coverage(db2, pr_number=7, head_sha=head, coverage=unparseable)
    row = fetch_spec_coverage(db2, 7, head_sha=head)[-1]
    assert row.covered is False
    reasons = compute_merge_decision(summarise_ledger_facts(db2, pr_number=7, head_sha=head))
    assert len(reasons.reasons) == 1
    assert reasons.reasons[0].startswith("spec plan unparseable at develop: ")
    assert _ABSENT not in reasons.reasons[0]
    assert not any(_HEADER in reason for reason in reasons.reasons)


def test_plan_file_without_the_marker_is_not_an_action_plan(tmp_path: Path) -> None:
    plan_path = f"docs/plans/{_ID}.md"
    spec = _spec("approved", _criteria(*_PRESENT, _ABSENT))
    repo, head = _repo(
        tmp_path,
        base={_SPEC_PATH: spec, plan_path: "# Plan\n\nProse steps, no marker.\n"},
        head={_CODE: "VALUE = 1\n", **_present_tests()},
    )
    changed = [_CODE, "tests/unit/test_planless.py"]

    coverage, _ = coverage_at_head(repo, spec_id=_ID, base_ref="develop", changed_files=changed)

    assert coverage.contract == "acceptance-criteria"
    assert coverage.missing == [_ABSENT]
    assert coverage.report == [
        f"plan-absent: graded against the acceptance criteria of {_ID} (3 selectors)",
        f"plan file present but not an action plan: {plan_path}",
    ]
    db = _ledger(tmp_path, head)
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=coverage)
    reasons = compute_merge_decision(summarise_ledger_facts(db, pr_number=7, head_sha=head))
    assert reasons.reasons == [f"{_HEADER}: {_ABSENT} (AC3)"]
    assert not any("unparseable" in reason for reason in reasons.reasons)


def test_stored_plan_absent_row_is_regraded_by_the_new_rule(tmp_path: Path) -> None:
    spec = _spec("approved", _criteria(*_PRESENT))
    repo, head = _repo(
        tmp_path, base={_SPEC_PATH: spec}, head={_CODE: "VALUE = 1\n", **_present_tests()}
    )
    changed = [_CODE, "tests/unit/test_planless.py"]
    db = _ledger(tmp_path, head)
    stale = SpecCoverage(
        spec_id=_ID,
        n_promised=0,
        n_present=0,
        missing=["plan-absent"],
        covered=False,
        rule_version=1,
    )
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=stale)
    assert compute_merge_decision(summarise_ledger_facts(db, pr_number=7, head_sha=head)).reasons

    _, decision = _decide(repo, db, head, changed)

    latest = fetch_spec_coverage(db, 7, head_sha=head)[-1]
    assert SPEC_COVERAGE_RULE_VERSION == 2
    assert latest.rule_version == 2
    assert latest.contract == "acceptance-criteria"
    assert decision.merge is True, decision.reasons


def test_bare_node_id_and_test_file_path_are_selectors() -> None:
    markdown = (
        "## Acceptance Criteria\n\n"
        "- [ ] AC1: `tests/x.py::test_y` -> green.\n"
        "- [ ] AC2: `tests/z.py` -> green.\n"
        "- [ ] AC3: `src/repoach/a.py` changes.\n\n"
        "## Open Questions\n"
    )
    found = [(entry.selector, entry.origin) for entry in spec_acceptance_criteria(markdown)]
    assert found == [("tests/x.py::test_y", "AC1"), ("tests/z.py", "AC2")]


def test_fixture_prefix_and_fixtures_tree_are_examples_not_selectors() -> None:
    markdown = (
        "## Acceptance Criteria\n\n"
        "- [ ] AC1: `pytest -q tests/unit/test_real.py::test_it` -> green, with "
        "`fixture:tests/x.py::test_y` absent.\n"
        "- [ ] AC2: `tests/fixtures/x/test_y.py::test_y` and "
        "`pytest -q tests/fixtures/x/test_z.py` are fixtures.\n\n"
        "## Open Questions\n"
    )
    found = [(entry.selector, entry.origin) for entry in spec_acceptance_criteria(markdown)]
    assert found == [("tests/unit/test_real.py::test_it", "AC1")]


def test_a_published_grading_error_carries_no_credential() -> None:
    from repoach.review.spec_grading import StaleCoverageRecomputeError, redact_error

    token = "ghp_" + "a1" * 12
    assert redact_error(f"fatal: https://bot:{token}@github.com/o/r") == (
        "fatal: https://***@github.com/o/r"
    )
    assert redact_error(f"auth failed with {token}") == "auth failed with ***"
    assert redact_error("fatal: https://bot:pa/ss@github.com/o/r failed") == (
        "fatal: https://***@github.com/o/r failed"
    )
    assert redact_error("fatal: https://bot:p@ss@github.com/o/r failed") == (
        "fatal: https://***@github.com/o/r failed"
    )
    assert redact_error("see https://github.com/o/r and mail a@b") == (
        "see https://github.com/o/r and mail a@b"
    )
    assert redact_error("git show failed: no such path docs/plans/SP-X.md") == (
        "git show failed: no such path docs/plans/SP-X.md"
    )
    assert redact_error("glpat-" + "c3" * 10) == "***"
    stale = StaleCoverageRecomputeError(
        head_sha="abcdef1234", rule_version=1, error=RuntimeError(f"https://x:{token}@h/r")
    )
    assert token not in stale.reason
    assert "https://***@h/r" in stale.reason


def test_a_marked_plan_new_at_head_replaces_a_prose_plan_at_base(tmp_path: Path) -> None:
    plan_path = f"docs/plans/{_ID}.md"
    spec = _spec("approved", _criteria(_ABSENT))
    repo, _ = _repo(
        tmp_path,
        base={_SPEC_PATH: spec, plan_path: "# Plan\n\nProse steps, no marker.\n"},
        head={plan_path: _plan(_PLAN_SELECTOR), "tests/p.py": _tests_file("test_p")},
    )

    coverage, resolution = coverage_at_head(
        repo, spec_id=_ID, base_ref="develop", changed_files=[plan_path, "tests/p.py"]
    )

    assert resolution.fell_back_to_head is True
    assert resolution.prose_plan == ""
    assert coverage.contract == "plan"
    assert coverage.covered is True
    assert coverage.missing == []


def _row(spec_id: str, *, batch: str, covered: bool) -> SpecCoverage:
    return SpecCoverage(
        spec_id=spec_id,
        n_promised=1,
        n_present=int(covered),
        missing=[],
        covered=covered,
        batch=batch,
    )


def test_latest_batch_folds_only_the_last_grading() -> None:
    legacy = _row("SP-A", batch="", covered=True)
    first = [_row("SP-A", batch="b1", covered=False), _row("SP-B", batch="b1", covered=False)]
    second = [
        _row("SP-A", batch="b2", covered=False),
        _row("SP-B", batch="b2", covered=True),
        _row("SP-A", batch="b2", covered=True),
    ]

    assert latest_batch([]) == []
    assert latest_batch([*first, legacy]) == [legacy]
    folded = latest_batch([legacy, *first, *second])
    assert [(row.spec_id, row.batch, row.covered) for row in folded] == [
        ("SP-A", "b2", True),
        ("SP-B", "b2", True),
    ]


@pytest.mark.parametrize(
    ("criterion", "expected"),
    [
        ("`pytest -q -n 8 tests/unit/test_a.py::test_b`", ["tests/unit/test_a.py::test_b"]),
        ("`pytest -q -n 8`", []),
        ("`pytest tests/unit/test_a-b.py`", ["tests/unit/test_a-b.py"]),
        (
            "`tests/unit/test_a.py::TestX::test_y[case-1]`",
            ["tests/unit/test_a.py::TestX::test_y[case-1]"],
        ),
        ("`src/repoach/x.py`", []),
        ("`src/repoach/x.py::helper`", ["src/repoach/x.py::helper"]),
        ("`lib/test_tool.py`", ["lib/test_tool.py"]),
        ("`lib/tool_test.py`", ["lib/tool_test.py"]),
        ("`` `tests/x.py::test_y` ``", []),
        ("`fixture:tests/x.py::test_y`", []),
        ("`tests/fixtures/x/test_y.py::test_y`", []),
        ("`pytest tests/fixtures/x/test_z.py tests/unit/test_c.py`", ["tests/unit/test_c.py"]),
        ("`tests/ünï/test_a.py`", ["tests/ünï/test_a.py"]),
        ("`tests/x.py and more`", []),
    ],
)
def test_span_selector_forms(criterion: str, expected: list[str]) -> None:
    markdown = f"## Acceptance Criteria\n\n- [ ] AC1: {criterion} -> green.\n"
    assert [entry.selector for entry in spec_acceptance_criteria(markdown)] == expected


def test_a_spec_without_frontmatter_is_refused_naming_it(tmp_path: Path) -> None:
    prose_path = "docs/specs/2026-10-27_SP-PROSE_prose.md"
    repo, _ = _repo(
        tmp_path,
        base={prose_path: "# Prose\n\n## Acceptance Criteria\n\n- [ ] AC1: `tests/a.py`\n"},
        head={_CODE: "VALUE = 1\n"},
    )

    coverage, _ = coverage_at_head(
        repo, spec_id="SP-PROSE", base_ref="develop", changed_files=[_CODE]
    )

    assert coverage.contract == "none"
    assert coverage.covered is False
    assert coverage.draft_refusals == ["spec coverage not graded: SP-PROSE has no frontmatter"]


@pytest.mark.parametrize(
    "markdown",
    [
        "# Spec\n\nNo criteria section.\n",
        "# Spec\n\n## Acceptance Criteria\n\n## Open Questions\n",
        "## Acceptance Criteria\n\n- [ ] AC1: prose only, `src/x.py` changes.\n",
    ],
)
def test_an_absent_or_empty_ac_section_names_no_selector(markdown: str) -> None:
    assert spec_acceptance_criteria(markdown) == []


def test_an_unparseable_plan_error_is_published_without_its_credential(tmp_path: Path) -> None:
    token = "ghp_" + "d4" * 12
    corrupt = (
        "# Plan\n\n<!-- repoach-action-plan -->\n```json\n"
        f'{{"spec_id": "https://bot:{token}@github.com/o/r"}}\n```\n'
    )
    repo, _ = _repo(
        tmp_path,
        base={_SPEC_PATH: _spec("approved", _criteria(_ABSENT)), f"docs/plans/{_ID}.md": corrupt},
        head={_CODE: "VALUE = 1\n"},
    )

    coverage, resolution = coverage_at_head(
        repo, spec_id=_ID, base_ref="develop", changed_files=[_CODE]
    )

    assert token not in resolution.error
    (refusal,) = coverage.draft_refusals
    assert refusal.startswith("spec plan unparseable at develop: ")
    assert token not in refusal
    assert "https://***@github.com/o/r" in refusal
