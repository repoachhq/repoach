"""SP-PLAN-SELECTOR-AUDIT-WIRE 0.2 — a draft spec's absent selectors are planned.

A spec-only PR for a ``status: draft`` spec names tests that exist only
once the spec is implemented. Their selectors are reported as
``planned`` and do not refuse the merge; any other status refuses as
before, and a draft PR that also changes code the spec owns, or a spec
past draft at base, loses the exemption with the rule named.
"""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace

from repoach.review.findings import init_findings_schema, record_review_integrity
from repoach.review.governed_spec import load_governed_spec_at_ref
from repoach.review.merge_gate import compute_merge_decision, summarise_ledger_facts
from repoach.review.orchestrator import ReviewTeamOrchestrator
from repoach.review.spec_gate import (
    coverage_of_selectors,
    draft_scope,
    fetch_spec_coverage,
    init_spec_coverage_schema,
    record_spec_coverage,
    spec_acceptance_selectors,
)

_SPEC_PATH = "docs/specs/2026-10-25_SP-DRAFTY_drafty.md"
_OWNED = "src/repoach/review/drafty.py"
_SELECTORS = [
    "tests/unit/test_drafty.py::test_first",
    "tests/unit/test_drafty.py::test_second",
]
_REASON = "spec acceptance selectors not all present"
_HEAD = "head0001"


def _spec(status: str) -> str:
    return (
        "---\n"
        "id: SP-DRAFTY\n"
        "title: Drafty\n"
        "version: 0.1\n"
        f"status: {status}\n"
        "owns:\n"
        f"  code: [{_OWNED}, tests/unit/test_drafty.py]\n"
        "  resources: []\n"
        "depends_on: []\n"
        "---\n\n"
        "# Drafty\n\n"
        "## Behavior\n\n"
        "`pytest -q tests/unit/test_not_an_ac.py::test_x` is prose, not a criterion.\n\n"
        "## Acceptance Criteria\n\n"
        f"- [ ] AC1: `pytest -q {_SELECTORS[0]}` -> green.\n"
        f"- [ ] AC2: `pytest -q -n 8 {_SELECTORS[1]}` -> green, and again "
        f"`pytest -q {_SELECTORS[0]}`.\n\n"
        "## Open Questions\n\nNone.\n"
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True)


def _repo(tmp_path: Path, *, base_status: str | None, head_status: str) -> Path:
    repo = tmp_path / "repo"
    (repo / "docs/specs").mkdir(parents=True)
    _git(repo, "init", "-q", "-b", "develop")
    _git(repo, "config", "user.email", "test@example.invalid")
    _git(repo, "config", "user.name", "Test Runner")
    (repo / "README.md").write_text("base\n", encoding="utf-8")
    if base_status is not None:
        (repo / _SPEC_PATH).write_text(_spec(base_status), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    _git(repo, "checkout", "-q", "-b", "docs/sp-drafty")
    (repo / _SPEC_PATH).write_text(_spec(head_status), encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    return repo


def _scope(repo: Path, changed: list[str]) -> tuple[object, str]:
    stub = SimpleNamespace(_repo_root=repo)
    return ReviewTeamOrchestrator._draft_scope(
        stub, "SP-DRAFTY", base_ref="develop", changed_files=changed
    )


def _gate(repo: Path, tmp_path: Path, changed: list[str]) -> tuple[object, object]:
    scope, markdown = _scope(repo, changed)
    coverage = coverage_of_selectors(
        repo,
        spec_id="SP-DRAFTY",
        selectors=spec_acceptance_selectors(markdown or _spec("approved")),
        draft=scope,
    )
    db = tmp_path / "ledger.db"
    init_findings_schema(db)
    record_review_integrity(db, pr_number=7, head_sha=_HEAD, n_reviewers=4, n_unparsed=0)
    record_spec_coverage(db, pr_number=7, head_sha=_HEAD, coverage=coverage)
    facts = summarise_ledger_facts(db, pr_number=7, head_sha=_HEAD)
    return facts, compute_merge_decision(facts)


def test_spec_acceptance_selectors_reads_only_the_ac_section() -> None:
    assert spec_acceptance_selectors(_spec("draft")) == _SELECTORS


def test_draft_spec_absent_selectors_are_planned_and_merge(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="draft")
    facts, decision = _gate(repo, tmp_path, [_SPEC_PATH])
    assert decision.merge is True
    assert decision.reasons == []
    assert facts.spec_planned == _SELECTORS
    assert facts.spec_missing == []


def test_active_spec_absent_selectors_refuse_as_today(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="approved")
    scope, _ = _scope(repo, [_SPEC_PATH])
    assert scope is None
    facts, decision = _gate(repo, tmp_path, [_SPEC_PATH])
    assert decision.merge is False
    assert decision.reasons == [_REASON]
    assert facts.spec_missing == _SELECTORS
    assert facts.spec_planned == []


def test_draft_spec_touching_owned_code_is_refused_naming_the_rule(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="draft")
    facts, decision = _gate(repo, tmp_path, [_SPEC_PATH, _OWNED])
    assert decision.merge is False
    assert decision.reasons == [
        _REASON,
        f"draft spec PR touches code outside docs ({_OWNED}): "
        "its acceptance selectors must be present",
    ]
    assert facts.spec_missing == _SELECTORS
    assert facts.spec_planned == []


def test_draft_spec_touching_unrelated_code_is_refused(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="draft")
    unrelated = "src/repoach/core/unrelated.py"
    facts, decision = _gate(repo, tmp_path, [_SPEC_PATH, "docs/notes.md", unrelated])
    assert decision.merge is False
    assert decision.reasons == [
        _REASON,
        f"draft spec PR touches code outside docs ({unrelated}): "
        "its acceptance selectors must be present",
    ]
    assert facts.spec_planned == []


def test_draft_spec_with_other_docs_keeps_the_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="draft")
    facts, decision = _gate(repo, tmp_path, [_SPEC_PATH, "docs/notes.md"])
    assert decision.merge is True
    assert facts.spec_planned == _SELECTORS


def test_owned_path_under_docs_withholds_the_exemption() -> None:
    owned = "docs/prompts/drafty.md"
    scope = draft_scope(
        head_status="draft",
        base_status=None,
        owns_code=(owned,),
        changed_files=[_SPEC_PATH, owned],
        spec_path=_SPEC_PATH,
    )
    assert scope is not None
    assert scope.refusals == (
        f"draft spec PR touches owned code ({owned}): its acceptance selectors must be present",
    )


def test_spec_approved_at_base_cannot_step_back_to_draft(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status="approved", head_status="draft")
    _, decision = _gate(repo, tmp_path, [_SPEC_PATH])
    assert decision.merge is False
    assert any("'approved' at base" in reason for reason in decision.reasons)


def test_draft_at_base_and_head_keeps_the_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status="draft", head_status="draft")
    _, decision = _gate(repo, tmp_path, [_SPEC_PATH])
    assert decision.merge is True


def test_unreadable_base_ref_withholds_the_exemption(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status=None, head_status="draft")
    stub = SimpleNamespace(_repo_root=repo)
    scope, _ = ReviewTeamOrchestrator._draft_scope(
        stub, "SP-DRAFTY", base_ref="no-such-branch", changed_files=[_SPEC_PATH]
    )
    assert scope is not None
    assert scope.allows_planned is False
    assert "unreadable" in scope.refusals[0]


def test_draft_selector_in_an_existing_file_stays_missing(tmp_path: Path) -> None:
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/test_drafty.py").write_text(
        "def test_first():\n    assert 1 == 1\n", encoding="utf-8"
    )
    scope = draft_scope(
        head_status="draft",
        base_status=None,
        owns_code=(),
        changed_files=[],
        spec_path=_SPEC_PATH,
    )
    coverage = coverage_of_selectors(
        tmp_path, spec_id="SP-DRAFTY", selectors=_SELECTORS, draft=scope
    )
    assert coverage.missing == [_SELECTORS[1]]
    assert coverage.planned == []
    assert coverage.n_present == 1
    assert coverage.covered is False


def test_load_governed_spec_at_ref_reads_base_status(tmp_path: Path) -> None:
    repo = _repo(tmp_path, base_status="approved", head_status="draft")
    base = load_governed_spec_at_ref("SP-DRAFTY", "develop", root=repo)
    assert base is not None
    assert base.status == "approved"
    assert base.owns_code == (_OWNED, "tests/unit/test_drafty.py")


def test_ledger_round_trips_planned_and_adds_columns_to_an_old_table(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.execute(
            "CREATE TABLE pr_spec_coverage (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "pr_number INTEGER NOT NULL, head_sha TEXT NOT NULL, spec_id TEXT NOT NULL, "
            "n_promised INTEGER NOT NULL, n_present INTEGER NOT NULL, "
            "missing TEXT NOT NULL, covered INTEGER NOT NULL)"
        )
        conn.execute(
            "INSERT INTO pr_spec_coverage (pr_number, head_sha, spec_id, n_promised, "
            "n_present, missing, covered) VALUES (1, 'old', 'SP-X', 1, 1, '', 1)"
        )
    init_spec_coverage_schema(db)
    scope = draft_scope(
        head_status="draft",
        base_status="approved",
        owns_code=(_OWNED,),
        changed_files=[_OWNED],
        spec_path=_SPEC_PATH,
    )
    coverage = coverage_of_selectors(tmp_path, spec_id="SP-X", selectors=_SELECTORS, draft=scope)
    record_spec_coverage(db, pr_number=1, head_sha="new", coverage=coverage)
    old, new = fetch_spec_coverage(db, 1)
    assert old.planned == [] and old.draft_refusals == [] and old.covered is True
    assert new.draft_refusals == list(scope.refusals)
    assert len(new.draft_refusals) == 2
