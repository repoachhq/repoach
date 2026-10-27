"""SP-GATE-REVERIFIES-AT-HEAD: the gate re-checks findings of older heads at the head.

A real git origin carries ``refs/pull/7/head``. Head A plants an
f-string SQL query; a blocking ``security`` finding is recorded
``verified`` at A, the way the refuter leaves it after the review of A.
A person pushes head B. The gate at B must re-check the finding against
B's tree (the refuter re-judges it there), settle it when B no longer
supports it, and name it when B still does.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult, GitHubForge
from repoach.forge.protocol import BranchProtection, Comment, MergeMethod, PullRequest
from repoach.gates.config import REFUTER_CONTEXT_CEILING_DEFAULT, REFUTER_CONTEXT_TOKENS_FLOOR
from repoach.review import auto_merge, finding_verifiers, head_reverify
from repoach.review.auto_merge import CIGateOutcome, GateEvaluation, evaluate_merge_gate
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
    record_review_integrity,
    record_verification_attempt,
)
from repoach.review.head_reverify import REVERIFY_METHOD
from repoach.review.merge_gate import MergeDecision, MergeFacts

_PLANTED = (
    "def search(db, prefix):\n"
    '    """Return the customers starting with prefix."""\n'
    "    return db.execute(f\"SELECT * FROM t WHERE c LIKE '{prefix}%'\")\n"
)
_FIXED = (
    "def search(db, prefix):\n"
    '    """Return the customers starting with prefix."""\n'
    '    return db.execute("SELECT * FROM t WHERE substr(c, 1, length(?)) = ?", (prefix, prefix))\n'
)
_STILL_PLANTED = _PLANTED + '\n\ndef count(db):\n    """Count rows."""\n    return 0\n'
_TS_SOURCE = "export function dueTotal(cents: number[]): number {\n  return 0;\n}\n"
_TS_TEST = (
    'import { expect, it } from "vitest";\n'
    'import { dueTotal } from "./t";\n\n'
    'it("sums", () => {\n  expect(dueTotal([])).toBe(0);\n});\n'
)


def _git(cwd: Path, *args: str) -> str:
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        "HOME": str(cwd),
    }
    proc = subprocess.run(
        ["git", *args], cwd=cwd, env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


class _Repo:
    """A seed clone that pushes heads to ``refs/pull/7/head`` of a bare origin."""

    def __init__(self, tmp_path: Path) -> None:
        self.origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(self.origin))
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        (self.seed / "README.md").write_text("demo\n", encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", "base")
        _git(self.seed, "remote", "add", "origin", str(self.origin))
        _git(self.seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(self.origin), str(self.checkout))

    def push_head(self, files: dict[str, str], message: str) -> str:
        for name, content in files.items():
            target = self.seed / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", message)
        _git(self.seed, "push", "-q", "-f", "origin", "HEAD:refs/pull/7/head")
        return _git(self.seed, "rev-parse", "HEAD")


class _HeadForge(GitHubForge):
    """A :class:`GitHubForge` with a stubbed ``gh`` and a real ``git``."""

    def __init__(self, cwd: Path, head: str) -> None:
        super().__init__(cwd=cwd, gh_path="/nonexistent/gh", bot_login="bot")
        self._head = head

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        raise AssertionError(f"unexpected gh call: {args}")

    def _pr_head_sha(self, pr_number: int) -> str:
        return self._head

    def branch_protection(self, branch: str) -> BranchProtection:
        return BranchProtection(readable=False)

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        return None

    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        return Comment(id=1, author="bot", body=body)


class _Judge:
    """A refuter that refutes unless the evidence still interpolates the prefix."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        planted = "{prefix}" in prompt.split("<<<EVIDENCE", 1)[-1]
        verdict = {
            "refuted": not planted,
            "reasoning": "the prefix is still interpolated" if planted else "the prefix is bound",
        }
        return f"VERDICT: {json.dumps(verdict)}"


def _ci(monkeypatch: pytest.MonkeyPatch, *, green: bool) -> None:
    outcome = CIGateOutcome(
        green=green, outcome_tag="GREEN" if green else "SKIP_CI_PENDING", reason=""
    )
    monkeypatch.setattr(auto_merge, "evaluate_ci_gate", lambda *_a, **_k: outcome)


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Callable[..., GateEvaluation]:
    """Return a runner of the real gate at a head, with CI green and the head verified."""
    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: None)
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)

    def _run(
        repo: _Repo,
        head: str,
        judge_factory: Callable[[], Callable[[str], str]],
        *,
        ci_green: bool = True,
    ) -> GateEvaluation:
        monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (head, ""))
        _ci(monkeypatch, green=ci_green)
        return evaluate_merge_gate(
            7,
            forge=_HeadForge(repo.checkout, head),
            db_path=tmp_path / "state" / "review.db",
            repo_root=repo.checkout,
            pin_tree=True,
            judge_factory=judge_factory,
        )

    return _run


def _db(tmp_path: Path) -> Path:
    return tmp_path / "state" / "review.db"


def _record(
    db: Path,
    *,
    head: str,
    claim_type: ClaimType,
    file: str,
    line: int,
    claim: str,
    method: str,
) -> int:
    finding = Finding(
        pr_number=7,
        head_sha=head,
        round=1,
        finder="sentinel",
        claim_type=claim_type,
        severity=Severity.BLOCKING,
        file=file,
        line_start=line,
        line_end=line,
        claim=claim,
        evidence_pointer=f"{file}:{line}",
        status=FindingStatus.VERIFIED,
        verification_method=method,
        verification_result="confirmed at A",
        checked_at_sha=head,
    )
    init_findings_schema(db)
    return record_finding(db, finding)


def _planted(repo: _Repo, db: Path) -> tuple[str, int]:
    head_a = repo.push_head({"src/store.py": _PLANTED}, "feat: search")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.SECURITY,
        file="src/store.py",
        line=3,
        claim="search() interpolates prefix into SQL: injection",
        method="refuter",
    )
    record_review_integrity(db, pr_number=7, head_sha=head_a, n_reviewers=4, n_unparsed=0)
    return head_a, finding_id


def _review_at(db: Path, head: str) -> None:
    record_review_integrity(db, pr_number=7, head_sha=head, n_reviewers=4, n_unparsed=0)


def _row(db: Path, finding_id: int) -> Finding:
    return next(f for f in fetch_findings(db, 7) if f.id == finding_id)


def _never() -> Callable[[str], str]:
    raise AssertionError("the judge must not be built")


def test_human_fix_resolves_the_finding_and_the_gate_passes(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)
    judge = _Judge()

    evaluation = gate(repo, head_b, lambda: judge)

    row = _row(db, finding_id)
    assert row.status is FindingStatus.RESOLVED_AT_HEAD
    assert row.verification_result.startswith(f"resolved at {head_b[:7]}: ")
    assert row.checked_at_sha == head_b
    assert evaluation.facts.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert evaluation.decision.reasons == []
    assert evaluation.decision.merge is True
    assert judge.calls == 1


def test_fix_that_keeps_the_defect_is_named_still_present(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, finding_id = _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _STILL_PLANTED}, "feat: count")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _Judge)

    row = _row(db, finding_id)
    assert row.status is FindingStatus.VERIFIED
    assert row.verification_method == REVERIFY_METHOD
    assert row.checked_at_sha == head_b
    assert evaluation.decision.merge is False
    assert "1 open blocking finding(s) at head" in evaluation.decision.reasons
    named = [r for r in evaluation.decision.reasons if r.startswith(f"finding {finding_id}:")]
    assert named == [
        f"finding {finding_id}: security src/store.py:3 raised at {head_a[:7]}, "
        f"still present at {head_b[:7]} (the prefix is still interpolated)"
    ]
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 1, "needs_judgement": 0}


def test_unjudgeable_finding_is_needs_judgement_not_silent(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, finding_id = _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)

    def _no_provider() -> Callable[[str], str]:
        raise RuntimeError("no provider configured")

    evaluation = gate(repo, head_b, _no_provider)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 0, "needs_judgement": 1}
    assert evaluation.decision.merge is False
    assert (
        f"finding {finding_id}: security src/store.py:3 raised at {head_a[:7]}, "
        "unresolved: needs judgement (judge call failed: RuntimeError)"
    ) in evaluation.decision.reasons


def test_judging_cap_leaves_the_rest_needs_judgement(
    tmp_path: Path, gate: Callable[..., GateEvaluation], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, first = _planted(repo, db)
    second = _record(
        db,
        head=head_a,
        claim_type=ClaimType.DESIGN,
        file="src/store.py",
        line=3,
        claim="search() builds SQL by hand",
        method="refuter",
    )
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)
    monkeypatch.setenv("REPOACH_REFUTER_MAX_JUDGED", "1")

    evaluation = gate(repo, head_b, _Judge)

    assert _row(db, first).status is FindingStatus.RESOLVED_AT_HEAD
    assert _row(db, second).status is FindingStatus.VERIFIED
    assert evaluation.facts.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 1}
    assert any(
        r.endswith("unresolved: needs judgement (judging cap 1 reached)")
        for r in evaluation.decision.reasons
    )


def test_vitest_test_added_at_head_resolves_missing_test(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a = repo.push_head({"frontend/src/t.ts": _TS_SOURCE}, "feat: due total")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.MISSING_TEST,
        file="frontend/src/t.ts",
        line=1,
        claim="dueTotal has no test; add test_dueTotal_sums_only_unpaid",
        method="symbol_search",
    )
    head_b = repo.push_head({"frontend/src/t.test.ts": _TS_TEST}, "test: due total")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    row = _row(db, finding_id)
    assert row.status is FindingStatus.RESOLVED_AT_HEAD
    assert row.verification_result == (
        f"resolved at {head_b[:7]}: frontend/src/t.test.ts exercises dueTotal"
    )
    assert evaluation.decision.merge is True


def test_vitest_test_absent_keeps_missing_test_open(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a = repo.push_head({"frontend/src/t.ts": _TS_SOURCE}, "feat: due total")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.MISSING_TEST,
        file="frontend/src/t.ts",
        line=1,
        claim="dueTotal has no test; add test_dueTotal_sums_only_unpaid",
        method="symbol_search",
    )
    head_b = repo.push_head({"README.md": "demo, again\n"}, "docs: readme")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert evaluation.decision.merge is False
    assert any(
        r.endswith(f"still present at {head_b[:7]} (no test exercises dueTotal)")
        for r in evaluation.decision.reasons
    )


def test_second_gate_at_the_same_head_makes_no_model_call(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _STILL_PLANTED}, "feat: count")
    _review_at(db, head_b)
    first = _Judge()
    gate(repo, head_b, lambda: first)

    evaluation = gate(repo, head_b, _never)

    assert first.calls == 1
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 1, "needs_judgement": 0}
    assert evaluation.decision.merge is False


def test_finding_of_the_current_head_is_left_to_its_review(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, finding_id = _planted(repo, db)

    evaluation = gate(repo, head_a, _never)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 0, "needs_judgement": 0}
    assert evaluation.decision.reasons == ["1 open blocking finding(s) at head"]


def test_cited_file_removed_at_head_resolves(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _planted(repo, db)
    (repo.seed / "src" / "store.py").unlink()
    head_b = repo.push_head({"src/other.py": '"""Other."""\n'}, "refactor: drop search")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    assert _row(db, finding_id).verification_result == (
        f"resolved at {head_b[:7]}: src/store.py absent at {head_b[:7]}"
    )
    assert evaluation.decision.merge is True


def test_cli_prints_the_line_and_the_json_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    _git(project, "init", "-q", "-b", "main")
    monkeypatch.chdir(project)
    facts = MergeFacts(
        head_sha="b" * 40,
        ci_green=True,
        open_blocking_findings=0,
        spec_covered=True,
        spec_coverage_known=True,
        review_complete=True,
        review_integrity_known=True,
        reverified={"resolved": 2, "still_open": 1, "needs_judgement": 0},
        reverify_line=(
            "gate: re-verified 3 finding(s) of older heads at bbbbbbb: "
            "resolved 2, still open 1, needs judgement 0"
        ),
    )
    evaluation = GateEvaluation(
        head_sha="b" * 40,
        ci_gate=CIGateOutcome(green=True, outcome_tag="", reason=""),
        facts=facts,
        decision=MergeDecision(merge=False, reasons=["1 open blocking finding(s) at head"]),
    )
    monkeypatch.setattr(review_cmds, "evaluate_merge_gate", lambda pr_number, **_k: evaluation)

    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_gate(1, forge_name=None)

    out, err = capsys.readouterr()
    assert exc.value.exit_code == 5
    assert json.loads(out)["reverified"] == {"resolved": 2, "still_open": 1, "needs_judgement": 0}
    lines = [line for line in err.splitlines() if re.match(r"^gate: re-verified", line)]
    assert lines == [facts.reverify_line]


def _merge(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    repo: _Repo,
    head: str,
    judge_factory: Callable[[], Callable[[str], str]],
    *,
    pin_tree: bool,
) -> auto_merge.AutoMergeResult:
    base = auto_merge.get_settings().integration_branch
    pr = PullRequest(number=7, state="open", head_sha=head, head_ref="feat/x", base_ref=base)
    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: pr)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (head, ""))
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)
    _ci(monkeypatch, green=True)
    return auto_merge.run_auto_merge(
        7,
        forge=_HeadForge(repo.checkout, head),
        db_path=_db(tmp_path),
        repo_root=repo.checkout,
        merge_method=MergeMethod.SQUASH,
        delete_branch=True,
        pin_tree=pin_tree,
        judge_factory=judge_factory,
    )


def test_merge_re_verifies_on_the_head_tree_not_the_checkout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, finding_id = _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _STILL_PLANTED}, "feat: count")
    _review_at(db, head_b)
    assert not (repo.checkout / "src" / "store.py").exists()

    result = _merge(monkeypatch, tmp_path, repo, head_b, _Judge, pin_tree=True)

    row = _row(db, finding_id)
    assert row.status is FindingStatus.VERIFIED
    assert row.verification_result.startswith(f"still present at {head_b[:7]}")
    assert result.outcome == auto_merge.OUTCOME_SKIP_GATE
    assert f"raised at {head_a[:7]}, still present at {head_b[:7]}" in result.notes


def test_unpinned_decision_skips_the_re_check_and_keeps_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)

    result = _merge(monkeypatch, tmp_path, repo, head_b, _never, pin_tree=False)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert result.outcome == auto_merge.OUTCOME_SKIP_GATE
    assert "findings of older heads not re-verified (no head tree pinned)" in result.notes


def test_broken_behavior_follows_ci_at_the_same_head(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a = repo.push_head({"src/store.py": _FIXED}, "feat: search")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.BROKEN_BEHAVIOR,
        file="src/store.py",
        line=1,
        claim="CI failed: tests",
        method="ci",
    )
    head_b = repo.push_head({"README.md": "fixed\n"}, "fix: tests")
    _review_at(db, head_b)

    pending = gate(repo, head_b, _never, ci_green=False)
    green = gate(repo, head_b, _never, ci_green=True)

    assert pending.facts.reverified["still_open"] == 1
    assert green.facts.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert _row(db, finding_id).status is FindingStatus.RESOLVED_AT_HEAD
    assert green.decision.merge is True


def test_needs_judgement_is_not_re_judged_at_the_same_head(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)

    def _no_provider() -> Callable[[str], str]:
        raise RuntimeError("no provider configured")

    gate(repo, head_b, _no_provider)
    again = gate(repo, head_b, _never)

    assert again.facts.reverified == {"resolved": 0, "still_open": 0, "needs_judgement": 1}
    assert any(
        r.endswith("unresolved: needs judgement (judge call failed: RuntimeError)")
        for r in again.decision.reasons
    )


@pytest.mark.parametrize(
    "test_source",
    [
        'import { it } from "vitest";\n// TODO: test dueTotal\nit("adds", () => {\n  expect(1).toBe(1);\n});\n',
        'import { it } from "vitest";\nit.skip("dueTotal", () => {\n  dueTotal([]);\n});\n',
        'import { it } from "vitest";\nit.todo("dueTotal sums the unpaid invoices");\n',
        'export const covered = "dueTotal";\n',
    ],
    ids=["comment", "skip", "todo", "string"],
)
def test_a_mention_outside_a_running_test_keeps_missing_test_open(
    tmp_path: Path, gate: Callable[..., GateEvaluation], test_source: str
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a = repo.push_head({"frontend/src/t.ts": _TS_SOURCE}, "feat: due total")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.MISSING_TEST,
        file="frontend/src/t.ts",
        line=1,
        claim="dueTotal has no test",
        method="symbol_search",
    )
    head_b = repo.push_head({"frontend/src/t.test.ts": test_source}, "test: due total")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert evaluation.facts.reverified == {"resolved": 0, "still_open": 1, "needs_judgement": 0}


def test_identical_claims_of_several_reviewers_cost_one_judge_call(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, first = _planted(repo, db)
    second = _record(
        db,
        head=head_a,
        claim_type=ClaimType.SECURITY,
        file="src/store.py",
        line=3,
        claim="search()  interpolates prefix into SQL: injection",
        method="refuter",
    )
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)
    judge = _Judge()

    evaluation = gate(repo, head_b, lambda: judge)

    assert judge.calls == 1
    assert _row(db, first).status is FindingStatus.RESOLVED_AT_HEAD
    assert _row(db, second).status is FindingStatus.RESOLVED_AT_HEAD
    assert evaluation.facts.reverified == {"resolved": 2, "still_open": 0, "needs_judgement": 0}


_MIGRATION = '"""Relabel the generic board."""\n\n\ndef upgrade():\n    """Relabel."""\n'
_MIGRATION_TEST = '"""Migration tests."""\n\n\ndef test_relabels_generic_rows():\n    assert True\n'
_MIGRATION_CLAIM = "upgrade() is untested; add test_relabels_generic_rows"


def _missing_migration_test(repo: _Repo, db: Path) -> tuple[str, int]:
    head_a = repo.push_head({"api/alembic/versions/n0_relabel.py": _MIGRATION}, "feat: relabel")
    finding_id = _record(
        db,
        head=head_a,
        claim_type=ClaimType.MISSING_TEST,
        file="api/alembic/versions/n0_relabel.py",
        line=4,
        claim=_MIGRATION_CLAIM,
        method="symbol_search",
    )
    return head_a, finding_id


def test_python_test_written_under_a_nested_tests_dir_resolves_missing_test(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _missing_migration_test(repo, db)
    head_b = repo.push_head({"api/tests/test_relabel.py": _MIGRATION_TEST}, "test: relabel")
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    row = _row(db, finding_id)
    assert row.status is FindingStatus.RESOLVED_AT_HEAD
    assert row.verification_result == (
        f"resolved at {head_b[:7]}: test_relabels_generic_rows exists at api/tests/test_relabel.py:4"
    )
    assert evaluation.decision.merge is True


def test_python_test_absent_at_head_names_what_was_searched(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _missing_migration_test(repo, db)
    head_b = repo.push_head(
        {"api/tests/test_other.py": '"""Other."""\n\n\ndef test_other():\n    assert True\n'},
        "test: other",
    )
    _review_at(db, head_b)

    evaluation = gate(repo, head_b, _never)

    assert _row(db, finding_id).status is FindingStatus.VERIFIED
    assert evaluation.decision.merge is False
    assert any(
        r.endswith(
            f"still present at {head_b[:7]} (no test for test_relabels_generic_rows "
            f"(searched 1 test files at {head_b[:7]}))"
        )
        for r in evaluation.decision.reasons
    )


def test_stale_still_present_stamp_at_the_head_yields_to_the_test_found_there(
    tmp_path: Path, gate: Callable[..., GateEvaluation]
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _, finding_id = _missing_migration_test(repo, db)
    head_b = repo.push_head({"api/tests/test_relabel.py": _MIGRATION_TEST}, "test: relabel")
    _review_at(db, head_b)
    record_verification_attempt(
        db,
        finding_id,
        method=REVERIFY_METHOD,
        result=f"still present at {head_b[:7]}: no test for test_relabels_generic_rows",
        checked_at_sha=head_b,
    )

    evaluation = gate(repo, head_b, _never)

    assert _row(db, finding_id).status is FindingStatus.RESOLVED_AT_HEAD
    assert evaluation.facts.reverified == {"resolved": 1, "still_open": 0, "needs_judgement": 0}
    assert evaluation.decision.merge is True


def test_two_missing_tests_at_one_head_list_the_tree_once(
    tmp_path: Path, gate: Callable[..., GateEvaluation], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    head_a, first = _missing_migration_test(repo, db)
    second = _record(
        db,
        head=head_a,
        claim_type=ClaimType.MISSING_TEST,
        file="api/alembic/versions/n0_relabel.py",
        line=5,
        claim="downgrade is untested; add test_downgrade_restores_noted_rows",
        method="symbol_search",
    )
    head_b = repo.push_head({"api/tests/test_relabel.py": _MIGRATION_TEST}, "test: relabel")
    _review_at(db, head_b)
    listings: list[Path] = []
    real = finding_verifiers._tracked_files

    def _counting(root: Path) -> list[str] | None:
        listings.append(root)
        return real(root)

    monkeypatch.setattr(finding_verifiers, "_tracked_files", _counting)

    evaluation = gate(repo, head_b, _never)

    assert len(listings) == 1
    assert _row(db, first).status is FindingStatus.RESOLVED_AT_HEAD
    assert _row(db, second).status is FindingStatus.VERIFIED
    assert evaluation.facts.reverified == {"resolved": 1, "still_open": 1, "needs_judgement": 0}


def test_re_check_fills_the_configured_refuter_context(
    tmp_path: Path, gate: Callable[..., GateEvaluation], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _planted(repo, db)
    head_b = repo.push_head({"src/store.py": _FIXED}, "fix: bind the prefix")
    _review_at(db, head_b)
    seen: list[int] = []
    real = head_reverify.judge_finding

    def spy(finding: Finding, **kwargs: object) -> object:
        seen.append(int(kwargs["context_tokens"]))
        return real(finding, **kwargs)

    monkeypatch.setattr(head_reverify, "judge_finding", spy)
    monkeypatch.setenv("REPOACH_REFUTER_CONTEXT_TOKENS", "150000")

    evaluation = gate(repo, head_b, _Judge)

    assert seen == [150000]
    assert evaluation.decision.merge is True


def test_merge_sizes_the_refuter_from_the_checkout_not_the_pr_head(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = _Repo(tmp_path)
    db = _db(tmp_path)
    _planted(repo, db)
    head_b = repo.push_head(
        {
            "src/store.py": _FIXED,
            "repoach.toml": "[gates]\nrefuter_context_tokens = 150000\n"
            "refuter_context_ceiling = 150000\n",
        },
        "fix: bind the prefix",
    )
    _review_at(db, head_b)
    seen: list[tuple[int, int]] = []
    real = head_reverify.judge_finding

    def spy(finding: Finding, **kwargs: object) -> object:
        seen.append((int(kwargs["context_tokens"]), int(kwargs["context_ceiling"])))
        return real(finding, **kwargs)

    monkeypatch.setattr(head_reverify, "judge_finding", spy)
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_TOKENS", raising=False)
    monkeypatch.delenv("REPOACH_REFUTER_CONTEXT_CEILING", raising=False)

    _merge(monkeypatch, tmp_path, repo, head_b, _Judge, pin_tree=True)

    assert seen == [(REFUTER_CONTEXT_TOKENS_FLOOR, REFUTER_CONTEXT_CEILING_DEFAULT)]
