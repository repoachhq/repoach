"""SP-FINDINGS-BRIDGE-DOCFIX 0.4 and SP-PLAN-SELECTOR-AUDIT-WIRE 0.3.

The spec coverage of a PR is derived data: a pure function of the spec,
the head tree and the diff's paths. A replayed ``review pr`` recomputes
it with the current rule and rewrites the row, without any model call,
and ``review gate`` recomputes a row whose ``rule_version`` predates the
current rule, so a row graded by retired code never refuses on its own.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from repoach.forge.protocol import ForgeWriteBarrier
from repoach.review import orchestrator
from repoach.review.auto_merge import decide_at_head
from repoach.review.findings import (
    init_findings_schema,
    record_review_integrity,
    record_review_run,
)
from repoach.review.merge_gate import compute_merge_decision, summarise_ledger_facts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.plan import ActionPlan, PlanStep, render_plan_markdown
from repoach.review.review_cache import completed_run
from repoach.review.reviewer import BotRole, ReviewerOutcome, ReviewVerdict
from repoach.review.spec_gate import (
    SPEC_COVERAGE_RULE_VERSION,
    SpecCoverage,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    record_spec_coverage,
)

_SPEC_PATH = "docs/specs/2026-10-25_SP-DRAFTY_drafty.md"
_SELECTORS = [
    "tests/unit/test_drafty.py::test_first",
    "tests/unit/test_drafty.py::test_second",
]
_REASON = "spec acceptance selectors not all present"
_STALE = SpecCoverage(
    spec_id="SP-DRAFTY",
    n_promised=0,
    n_present=0,
    missing=["base-ref-unavailable"],
    covered=False,
    rule_version=0,
)


def _spec(status: str) -> str:
    return (
        "---\n"
        "id: SP-DRAFTY\n"
        "title: Drafty\n"
        "version: 0.1\n"
        f"status: {status}\n"
        "owns:\n"
        "  code: [src/repoach/review/drafty.py, tests/unit/test_drafty.py]\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n"
        "# Drafty\n\n"
        "## Acceptance Criteria\n\n"
        f"- [ ] AC1: `pytest -q {_SELECTORS[0]}` -> green.\n"
        f"- [ ] AC2: `pytest -q {_SELECTORS[1]}` -> green.\n\n"
        "## Open Questions\n\nNone.\n"
    )


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)
    return proc.stdout.strip()


def _plan() -> str:
    return render_plan_markdown(
        ActionPlan(
            spec_id="SP-DRAFTY",
            title="Drafty",
            summary="Ship drafty.",
            steps=[
                PlanStep(
                    index=1,
                    title="Add drafty",
                    files=["tests/unit/test_drafty.py"],
                    action="Create the module.",
                    commit_message="feat(review): add drafty",
                    done_when="the acceptance tests are green",
                    unit_tests=_SELECTORS,
                )
            ],
        )
    )


def _repo(tmp_path: Path, status: str, *, plan: bool = False) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    (repo / "docs/specs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    if plan:
        (repo / "docs/plans").mkdir()
        (repo / "docs/plans/SP-DRAFTY.md").write_text(_plan(), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "docs/sp-drafty")
    (repo / _SPEC_PATH).write_text(_spec(status), encoding="utf-8")
    if plan:
        (repo / "tests/unit").mkdir(parents=True)
        (repo / "tests/unit/test_drafty.py").write_text(
            "def test_first():\n    assert 1 + 1 == 2\n", encoding="utf-8"
        )
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "head")
    return repo, _git(repo, "rev-parse", "HEAD")


class _Forge:
    """Serves the PR's base ref and diff paths; counts reads, refuses fetches."""

    def __init__(self) -> None:
        self.barrier = ForgeWriteBarrier()
        self.reads = 0

    def pull_request(self, pr_number: int) -> SimpleNamespace:
        self.reads += 1
        return SimpleNamespace(base_ref="develop")

    def diff(self, pr_number: int) -> SimpleNamespace:
        self.reads += 1
        return SimpleNamespace(
            text=f"diff --git a/{_SPEC_PATH} b/{_SPEC_PATH}\n--- /dev/null\n+++ b/{_SPEC_PATH}\n"
        )

    def git(self, args: list[str]) -> str:
        raise AssertionError(f"the base ref resolves locally, no fetch expected: {args}")


class _ModelCalls:
    """Every reviewer and judge entry point; any call is a counted model call."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, *_args: object, **_kwargs: object) -> object:
        self.calls += 1
        raise AssertionError("a replay must not call a model")


@pytest.fixture
def model_calls(monkeypatch: pytest.MonkeyPatch) -> _ModelCalls:
    counter = _ModelCalls()
    for name in ("Architect", "Sentinel", "Tester", "Scribe", "judge_findings_for_pr"):
        monkeypatch.setattr(orchestrator, name, counter)
    return counter


def _approve(role: BotRole) -> ReviewerOutcome:
    return ReviewerOutcome(
        role=role,
        verdict=ReviewVerdict.APPROVE,
        summary="ok",
        comments=[],
        model_used="stub",
        elapsed_s=0.0,
        tokens_used=0,
        raw_response="{}",
    )


def _cached_run_with_stale_row(db: Path, head: str, *, covered: bool) -> None:
    init_findings_schema(db)
    init_spec_coverage_schema(db)
    record_review_integrity(db, pr_number=7, head_sha=head, n_reviewers=4, n_unparsed=0)
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=_STALE)
    record_review_run(
        db,
        completed_run(
            run_id="run-1",
            pr_number=7,
            head_sha=head,
            final_verdict=ReviewVerdict.APPROVE,
            outcomes=[_approve(role) for role in BotRole],
            refutations=[],
            spec={
                "spec_id": "SP-DRAFTY",
                "spec_source": "branch",
                "spec_covered": covered,
                "spec_missing": False,
            },
        ),
    )


def _replay(repo: Path, db: Path, head: str, forge: _Forge) -> orchestrator.TeamOutcome:
    return ReviewTeamOrchestrator(
        forge=forge,
        db_path=db,
        post_to_github=False,
        repo_root=repo,
        head_sha=head,
        config_root=repo,
    ).review_pr(7)


def test_replay_recomputes_a_stale_row_of_a_draft_spec_and_the_gate_merges(
    tmp_path: Path, model_calls: _ModelCalls
) -> None:
    repo, head = _repo(tmp_path, "draft")
    db = tmp_path / "state" / "review.db"
    _cached_run_with_stale_row(db, head, covered=False)
    assert compute_merge_decision(summarise_ledger_facts(db, pr_number=7, head_sha=head)).reasons

    team = _replay(repo, db, head, _Forge())

    assert team.cached is True
    assert model_calls.calls == 0
    assert team.spec_covered is True
    assert team.spec_coverage_line == (
        f"review: spec coverage recomputed at {head[:7]}: planned 2, missing 0"
    )
    latest = fetch_spec_coverage(db, 7, head_sha=head)[-1]
    assert latest.planned == _SELECTORS
    assert latest.missing == []
    assert latest.rule_version == SPEC_COVERAGE_RULE_VERSION
    decision = compute_merge_decision(summarise_ledger_facts(db, pr_number=7, head_sha=head))
    assert decision.merge is True


def test_replay_of_an_approved_spec_with_missing_selectors_stays_refused(
    tmp_path: Path, model_calls: _ModelCalls
) -> None:
    repo, head = _repo(tmp_path, "approved", plan=True)
    db = tmp_path / "state" / "review.db"
    _cached_run_with_stale_row(db, head, covered=False)

    team = _replay(repo, db, head, _Forge())

    assert model_calls.calls == 0
    assert team.spec_covered is False
    assert team.spec_coverage_line == (
        f"review: spec coverage recomputed at {head[:7]}: planned 0, missing 1"
    )
    facts = summarise_ledger_facts(db, pr_number=7, head_sha=head)
    assert facts.spec_missing == [_SELECTORS[1]]
    assert compute_merge_decision(facts).reasons == [_REASON]


def test_gate_recomputes_a_row_graded_by_a_retired_rule(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path, "draft")
    db = tmp_path / "state" / "review.db"
    init_findings_schema(db)
    init_spec_coverage_schema(db)
    record_review_integrity(db, pr_number=7, head_sha=head, n_reviewers=4, n_unparsed=0)
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=_STALE)

    _, facts, decision = decide_at_head(
        7,
        forge=_Forge(),
        db=db,
        ci_green=True,
        repo_root=repo,
        head_sha=head,
        max_judged=0,
        head_tree=True,
        base_ref="develop",
    )

    assert decision.merge is True
    assert facts.spec_planned == _SELECTORS
    assert len(fetch_spec_coverage(db, 7, head_sha=head)) == 2


def test_gate_leaves_a_current_row_alone(tmp_path: Path) -> None:
    repo, head = _repo(tmp_path, "draft")
    db = tmp_path / "state" / "review.db"
    init_findings_schema(db)
    init_spec_coverage_schema(db)
    record_review_integrity(db, pr_number=7, head_sha=head, n_reviewers=4, n_unparsed=0)
    current = _STALE.model_copy(update={"rule_version": SPEC_COVERAGE_RULE_VERSION})
    record_spec_coverage(db, pr_number=7, head_sha=head, coverage=current)
    forge = _Forge()

    _, _, decision = decide_at_head(
        7,
        forge=forge,
        db=db,
        ci_green=True,
        repo_root=repo,
        head_sha=head,
        max_judged=0,
        head_tree=True,
        base_ref="develop",
    )

    assert decision.reasons == [_REASON]
    assert forge.reads == 0
    assert len(fetch_spec_coverage(db, 7, head_sha=head)) == 1


def test_rule_version_column_is_added_to_an_old_table_as_zero(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE pr_spec_coverage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, spec_id TEXT NOT NULL, "
            "n_promised INTEGER NOT NULL, n_present INTEGER NOT NULL, missing TEXT NOT NULL, "
            "covered INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO pr_spec_coverage (pr_number, head_sha, spec_id, n_promised, "
            "n_present, missing, covered) VALUES (7, 'h', 'SP-X', 0, 0, 'plan-absent', 0)"
        )
    init_spec_coverage_schema(db)
    assert [row.rule_version for row in fetch_spec_coverage(db, 7)] == [0]
