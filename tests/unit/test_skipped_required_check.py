"""A required check that never ran is a named failure, never green.

SP-SKIPPED-REQUIRED-CHECK-IS-NOT-GREEN, replayed on release PR #307's head
``79b9321`` (F3): the ``CI`` run 44071742332 (actor ``github-actions[bot]``)
concluded ``skipped`` and reported its matrix jobs under their template
names; the later run 44081266335 ran and every check is ``success``. The
two fixtures under ``tests/fixtures/check_runs/`` are those runs' check
runs, recorded from ``gh api repos/repoachhq/repoach/commits/<sha>/check-runs``
and reduced to the fields of D4.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.forge import CheckRun, Diff, PullRequest
from repoach.forge import github as forge_github
from repoach.forge.github import (
    REQUIRED_CHECK_FAIL_CONCLUSIONS,
    REQUIRED_CHECK_SUCCESS_CONCLUSIONS,
    SKIPPED_NOTE,
    GhResult,
    GitHubForge,
    classify_required_checks,
)
from repoach.review import merge_wait
from repoach.review.auto_merge import OUTCOME_SKIP_CI_FAILED, merge_exit_code, run_auto_merge
from repoach.review.coder_findings import run_coder_fix_from_findings
from repoach.review.coder_loop import CI_GREEN, CI_UNKNOWN, fetch_ci_status
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.merge_wait import (
    MERGE_ATTEMPTS_MARKER,
    MergeAttemptsComment,
    judge_required_checks,
    required_checks,
    template_of,
)
from tests.forge_fakes import scripted_github
from tests.merge_wait_fakes import FixturePR, MergeWaitFixtureForge, measured_era_root, seed_review

HEAD_79B = "79b9321150c9bb88aa79f6d96aeef238d9c45395"
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "check_runs"
SRC = Path(__file__).resolve().parents[2] / "src" / "repoach"

FIVE_NAMES: tuple[str, ...] = (
    "Test suite (Python 3.11)",
    "Test suite (Python 3.13)",
    "wheel-build",
    "wheel-smoke (3.11)",
    "wheel-smoke (3.13)",
)
"""The required names of the head's ``ci.yml`` at release #307, before ``gate`` (F4)."""

MATRIX_NAMES = [name for name in FIVE_NAMES if name != "wheel-build"]
WHEEL_BUILD_SKIPPED = f"wheel-build=SKIPPED ({SKIPPED_NOTE})"


def _load(kind: str) -> list[dict[str, str]]:
    return json.loads((FIXTURES / f"head_79b9321_{kind}.json").read_text(encoding="utf-8"))


def _check_runs(rows: list[dict[str, str]]) -> list[CheckRun]:
    return [
        CheckRun(
            name=row["name"],
            status=row["status"],
            conclusion=row["conclusion"] or "",
            started_at=row["started_at"] or "",
            completed_at=row["completed_at"] or "",
            head_sha=row["head_sha"],
            workflow=row["workflow_name"],
        )
        for row in rows
    ]


def _rollup(rows: list[dict[str, str]]) -> list[dict[str, object]]:
    return [
        {
            "name": row["name"],
            "status": row["status"],
            "conclusion": row["conclusion"],
            "startedAt": row["started_at"],
            "completedAt": row["completed_at"],
            "headSha": row["head_sha"],
        }
        for row in rows
    ]


def _template(name: str) -> str:
    if name.startswith("Test suite"):
        return "Test suite (Python ${{ matrix.python-version }})"
    return "wheel-smoke (${{ matrix.python-version }})"


def test_the_fixtures_are_the_two_runs_of_the_head() -> None:
    skipped, green = _load("skipped"), _load("green")
    assert {row["head_sha"] for row in skipped + green} == {HEAD_79B}
    assert {row["workflow_name"] for row in skipped + green} == {"CI"}
    assert {row["conclusion"] for row in skipped} == {"skipped"}
    assert {row["conclusion"] for row in green} == {"success"}
    assert sorted(row["name"] for row in skipped) == [
        "Test suite (Python ${{ matrix.python-version }})",
        "wheel-build",
        "wheel-build",
        "wheel-smoke (${{ matrix.python-version }})",
        "wheel-smoke (${{ matrix.python-version }})",
    ]
    assert sorted(row["name"] for row in green) == sorted(FIVE_NAMES)
    for row in skipped + green:
        assert set(row) == {
            "name",
            "status",
            "conclusion",
            "started_at",
            "completed_at",
            "head_sha",
            "workflow_name",
        }


def test_t1_judge_names_the_skipped_run() -> None:
    verdict = judge_required_checks(_check_runs(_load("skipped")), FIVE_NAMES, HEAD_79B)

    assert verdict.state == "failed"
    assert verdict.failed == [WHEEL_BUILD_SKIPPED]
    assert verdict.missing == [
        f'{name}: missing; the job reported unexpanded as "{_template(name)}"=SKIPPED'
        for name in MATRIX_NAMES
    ]
    assert all("${{ matrix.python-version }}" in line for line in verdict.missing)
    note = verdict.note()
    assert note.startswith(f"required_check_failed: {WHEEL_BUILD_SKIPPED}, ")
    for line in verdict.missing:
        assert line in note


def test_t2_classify_names_the_skipped_run() -> None:
    failed, pending, missing = classify_required_checks(_rollup(_load("skipped")), FIVE_NAMES)

    assert failed == [WHEEL_BUILD_SKIPPED]
    assert pending == []
    assert missing == MATRIX_NAMES


def test_t3_the_later_green_run_wins_per_name() -> None:
    green = _load("green")
    both = _load("skipped") + green

    assert judge_required_checks(_check_runs(green), FIVE_NAMES, HEAD_79B).state == "green"
    assert judge_required_checks(_check_runs(both), FIVE_NAMES, HEAD_79B).state == "green"
    assert classify_required_checks(_rollup(green), FIVE_NAMES) == ([], [], [])
    assert classify_required_checks(_rollup(both), FIVE_NAMES) == ([], [], [])


def test_t4_one_constant_both_readers_import() -> None:
    assert not hasattr(merge_wait, "_SUCCESS_CONCLUSIONS")
    assert not hasattr(merge_wait, "_FAIL_CONCLUSIONS")
    assert not hasattr(forge_github, "_SUCCESS_CONCLUSIONS")
    assert not hasattr(forge_github, "_FAIL_CONCLUSIONS")
    assert merge_wait.REQUIRED_CHECK_SUCCESS_CONCLUSIONS is REQUIRED_CHECK_SUCCESS_CONCLUSIONS
    assert frozenset({"SUCCESS", "NEUTRAL"}) == REQUIRED_CHECK_SUCCESS_CONCLUSIONS
    assert "SKIPPED" in REQUIRED_CHECK_FAIL_CONCLUSIONS
    assert "CANCELLED" not in REQUIRED_CHECK_FAIL_CONCLUSIONS

    hits = [
        f"{path.relative_to(SRC)}:{number}"
        for path in sorted(SRC.rglob("*.py"))
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if '"SKIPPED"' in line
    ]
    assert len(hits) == 1, hits


def test_t5_neutral_is_green_and_cancelled_only_is_cancelled() -> None:
    name = ["wheel-build"]
    neutral = CheckRun(name=name[0], status="COMPLETED", conclusion="NEUTRAL", head_sha=HEAD_79B)
    assert judge_required_checks([neutral], name, HEAD_79B).state == "green"
    assert classify_required_checks(
        [{"name": name[0], "status": "COMPLETED", "conclusion": "NEUTRAL"}], name
    ) == ([], [], [])

    cancelled = CheckRun(
        name=name[0], status="COMPLETED", conclusion="CANCELLED", head_sha=HEAD_79B
    )
    verdict = judge_required_checks([cancelled], name, HEAD_79B)
    assert verdict.state == "cancelled"
    assert verdict.cancelled == name
    assert verdict.failed == []


def test_t6_template_of_matches_the_prefix_only() -> None:
    rollup = [row["name"] for row in _load("skipped")]

    assert template_of("wheel-smoke (3.11)", rollup) == "wheel-smoke (${{ matrix.python-version }})"
    assert template_of("Test suite (Python 3.13)", rollup) == (
        "Test suite (Python ${{ matrix.python-version }})"
    )
    assert template_of("gate", rollup) is None
    assert template_of("wheel-build", rollup) is None
    assert template_of("wheel-smoke (3.11)", ["wheel-smoke (3.11)", "wheel-build"]) is None


def test_a_skipped_required_check_without_matrix_fails() -> None:
    names = ["gate", "wheel-build"]
    runs = [
        CheckRun(name=n, status="COMPLETED", conclusion="SKIPPED", head_sha=HEAD_79B) for n in names
    ]
    verdict = judge_required_checks(runs, names, HEAD_79B)
    assert verdict.state == "failed"
    assert verdict.failed == [f"{n}=SKIPPED ({SKIPPED_NOTE})" for n in names]
    assert verdict.note() == (
        f"required_check_failed: gate=SKIPPED ({SKIPPED_NOTE}), "
        f"wheel-build=SKIPPED ({SKIPPED_NOTE})"
    )


def test_a_plain_missing_name_in_a_failed_verdict_reads_missing() -> None:
    names = ["wheel-build", "gate"]
    runs = [CheckRun(name="wheel-build", status="COMPLETED", conclusion="FAILURE")]
    verdict = judge_required_checks(runs, names, HEAD_79B)
    assert verdict.missing == ["gate"]
    assert verdict.note() == "required_check_failed: wheel-build=FAILURE, gate: missing"


def test_the_gate_records_skip_ci_failed_on_the_skipped_head(tmp_path: Path) -> None:
    root = measured_era_root(tmp_path)
    assert tuple(required_checks(root)) == FIVE_NAMES
    fx = MergeWaitFixtureForge(
        [
            FixturePR(
                307,
                HEAD_79B,
                "fix/replay-head-79b9321",
                check_runs=_rollup(_load("skipped")),
            )
        ]
    )
    db = tmp_path / "review.db"
    seed_review(db, 307, HEAD_79B)

    result = run_auto_merge(
        307,
        forge=fx.forge,
        db_path=db,
        repo_root=root,
        wait_seconds=0,
        trigger="merge-on-ci:44071742332",
        clock=fx.clock,
    )

    assert result.outcome == OUTCOME_SKIP_CI_FAILED
    assert merge_exit_code(result.outcome) == 5
    assert result.notes.startswith(f"required_check_failed: {WHEEL_BUILD_SKIPPED}, ")
    assert (
        "Test suite (Python 3.11): missing; the job reported unexpanded as "
        '"Test suite (Python ${{ matrix.python-version }})"=SKIPPED'
    ) in result.notes
    assert fx.merges == []
    marked = [c for c in fx.pr.comments if MERGE_ATTEMPTS_MARKER in str(c["body"])]
    assert len(marked) == 1
    block = re.search(r"```json\n(.*?)\n```", str(marked[0]["body"]), re.S)
    assert block is not None
    attempts = MergeAttemptsComment.model_validate_json(block.group(1)).attempts
    assert [a.outcome for a in attempts] == [OUTCOME_SKIP_CI_FAILED]
    assert "wheel-build=SKIPPED" in attempts[0].reason


@pytest.mark.parametrize("conclusion", sorted(REQUIRED_CHECK_FAIL_CONCLUSIONS))
def test_every_fail_conclusion_fails_both_readers(conclusion: str) -> None:
    name = ["wheel-build"]
    verdict = judge_required_checks(
        [CheckRun(name=name[0], status="COMPLETED", conclusion=conclusion)], name, HEAD_79B
    )
    assert verdict.state == "failed"
    failed, _, _ = classify_required_checks(
        [{"name": name[0], "status": "COMPLETED", "conclusion": conclusion}], name
    )
    assert failed == verdict.failed


_PR_CHECKS_BUCKET = {"success": "pass", "skipped": "skipping", "neutral": "pass"}


def _coder_forge(rows: list[dict[str, str]]) -> GitHubForge:
    """A real GitHub forge answering ``gh pr checks`` and the head's check-runs from ``rows``."""
    pr_checks = [
        {
            "name": row["name"],
            "state": row["conclusion"].upper(),
            "bucket": _PR_CHECKS_BUCKET[row["conclusion"]],
            "link": "",
            "workflow": row["workflow_name"],
        }
        for row in rows
    ]

    def _run_side(args: list[str]) -> GhResult:
        if args[:2] == ["pr", "checks"]:
            return GhResult(returncode=0, stdout=json.dumps(pr_checks), stderr="", argv=args)
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            lines = "\n".join(json.dumps(entry) for entry in _rollup(rows))
            return GhResult(returncode=0, stdout=lines, stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    return scripted_github(run=_run_side)


def test_the_coder_reader_never_reads_the_all_skipped_head_green() -> None:
    forge = _coder_forge(_load("skipped"))

    state, failed = fetch_ci_status(forge, 307, required=FIVE_NAMES, head_sha=HEAD_79B)

    assert (state, failed) == (CI_UNKNOWN, [])


def test_the_coder_reader_reads_the_later_green_run_green() -> None:
    green = fetch_ci_status(
        _coder_forge(_load("green")), 307, required=FIVE_NAMES, head_sha=HEAD_79B
    )
    both = fetch_ci_status(
        _coder_forge(_load("skipped") + _load("green")),
        307,
        required=FIVE_NAMES,
        head_sha=HEAD_79B,
    )

    assert green == (CI_GREEN, [])
    assert both == (CI_GREEN, [])


def test_the_coder_reader_reads_neutral_green() -> None:
    rows = [{**row, "conclusion": "neutral"} for row in _load("green")]
    state, _ = fetch_ci_status(_coder_forge(rows), 307, required=FIVE_NAMES, head_sha=HEAD_79B)
    assert state == CI_GREEN


def _ci_finding() -> Finding:
    return Finding(
        pr_number=307,
        head_sha=HEAD_79B,
        round=1,
        finder="ci",
        claim_type=ClaimType.BROKEN_BEHAVIOR,
        severity=Severity.BLOCKING,
        file="(ci):wheel-build",
        line_start=1,
        line_end=1,
        claim="CI check failed: wheel-build",
        evidence_pointer="(ci):wheel-build:1",
        status=FindingStatus.OPEN,
    )


def _coder_round(kind: str, tmp_path: Path) -> FindingStatus:
    db = tmp_path / f"{kind}.db"
    init_findings_schema(db)
    record_finding(db, _ci_finding())
    real = _coder_forge(_load(kind) if kind != "both" else _load("skipped") + _load("green"))
    forge = MagicMock()
    forge.name = "github"
    forge.pull_request.return_value = PullRequest(
        number=307, base_ref="develop", head_ref="fix/x", state="open", head_sha=HEAD_79B
    )
    forge.diff.return_value = Diff(text="diff")
    forge.checks.side_effect = real.checks
    forge.ci_status.side_effect = real.ci_status

    class _NoFixCoder:
        def respond_to_findings(self, **_kwargs: object) -> dict[str, object]:
            return {}

    run_coder_fix_from_findings(
        307, forge=forge, repo_root=measured_era_root(tmp_path), coder=_NoFixCoder(), db_path=db
    )
    return next(
        f.status for f in fetch_findings(db, 307) if f.claim_type is ClaimType.BROKEN_BEHAVIOR
    )


@pytest.mark.usefixtures("base_resolves")
def test_a_ci_finding_stays_open_on_the_all_skipped_head(tmp_path: Path) -> None:
    assert _coder_round("skipped", tmp_path) is FindingStatus.OPEN


@pytest.mark.usefixtures("base_resolves")
def test_a_ci_finding_resolves_once_the_head_ran_green(tmp_path: Path) -> None:
    assert _coder_round("both", tmp_path) is FindingStatus.RESOLVED
