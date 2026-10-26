"""Integration test for SP-GATE-REVERIFIES-AT-HEAD: a fix head settles every older finding.

A real bare origin carries ``refs/pull/7/head``. Head A plants an
f-string SQL query and an untested migration; the review of A leaves a
blocking ``security`` finding (refuter) and a blocking ``missing_test``
finding (symbol search), both ``verified``. Head B binds the parameter
and adds the test under a nested ``api/tests/`` directory. The real
gate at B re-checks both against B's pinned tree: the judged one costs
one judge call, the mechanical one none, and the PR merges. A second
gate at B reuses the stamps and builds no judge at all.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest

from repoach.forge.github import GhResult, GitHubForge
from repoach.review import auto_merge
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
)

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
_MIGRATION = '"""Relabel the generic board."""\n\n\ndef upgrade():\n    """Relabel."""\n'
_MIGRATION_TEST = '"""Migration tests."""\n\n\ndef test_relabels_generic_rows():\n    assert True\n'


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


class _Origin:
    """A seed clone pushing heads to ``refs/pull/7/head`` of a bare origin, and a checkout."""

    def __init__(self, tmp_path: Path) -> None:
        origin = tmp_path / "origin.git"
        _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
        self.seed = tmp_path / "seed"
        self.seed.mkdir()
        _git(self.seed, "init", "-q", "-b", "main")
        (self.seed / "README.md").write_text("demo\n", encoding="utf-8")
        _git(self.seed, "add", "-A")
        _git(self.seed, "commit", "-q", "-m", "base")
        _git(self.seed, "remote", "add", "origin", str(origin))
        _git(self.seed, "push", "-q", "origin", "main")
        self.checkout = tmp_path / "checkout"
        _git(tmp_path, "clone", "-q", str(origin), str(self.checkout))

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
    """A :class:`GitHubForge` whose ``gh`` is never called and whose ``git`` is real."""

    def __init__(self, cwd: Path, head: str) -> None:
        super().__init__(cwd=cwd, gh_path="/nonexistent/gh", bot_login="bot")
        self._head = head

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        raise AssertionError(f"unexpected gh call: {args}")

    def _pr_head_sha(self, pr_number: int) -> str:
        return self._head


class _Judge:
    """A refuter that refutes unless the evidence still interpolates the prefix."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, prompt: str) -> str:
        self.calls += 1
        planted = "{prefix}" in prompt.split("<<<EVIDENCE", 1)[-1]
        return "VERDICT: " + json.dumps({"refuted": not planted, "reasoning": "judged at head"})


def _verified(db: Path, head: str, claim_type: ClaimType, file: str, line: int, claim: str) -> int:
    method = "refuter" if claim_type is ClaimType.SECURITY else "symbol_search"
    return record_finding(
        db,
        Finding(
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
        ),
    )


def _gate(
    monkeypatch: pytest.MonkeyPatch,
    origin: _Origin,
    db: Path,
    head: str,
    judge_factory: Callable[[], Callable[[str], str]],
) -> GateEvaluation:
    monkeypatch.setattr(auto_merge, "_pull_request_or_none", lambda *_a: None)
    monkeypatch.setattr(auto_merge, "resolve_verified_head", lambda *_a, **_k: (head, ""))
    monkeypatch.setattr(
        auto_merge,
        "evaluate_ci_gate",
        lambda *_a, **_k: CIGateOutcome(green=True, outcome_tag="GREEN", reason=""),
    )
    return evaluate_merge_gate(
        7,
        forge=_HeadForge(origin.checkout, head),
        db_path=db,
        repo_root=origin.checkout,
        pin_tree=True,
        judge_factory=judge_factory,
    )


def _no_judge() -> Callable[[str], str]:
    raise AssertionError("the second gate at the same head must not build a judge")


def test_gate_at_the_fix_head_settles_judged_and_mechanical_findings_and_merges(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("REPOACH_REFUTER_MAX_JUDGED", raising=False)
    origin = _Origin(tmp_path)
    db = tmp_path / "state" / "review.db"
    init_findings_schema(db)
    head_a = origin.push_head(
        {"src/store.py": _PLANTED, "api/alembic/versions/n0_relabel.py": _MIGRATION},
        "feat: search and relabel",
    )
    security = _verified(
        db, head_a, ClaimType.SECURITY, "src/store.py", 3, "search() interpolates prefix into SQL"
    )
    missing = _verified(
        db,
        head_a,
        ClaimType.MISSING_TEST,
        "api/alembic/versions/n0_relabel.py",
        4,
        "upgrade() is untested; add test_relabels_generic_rows",
    )
    record_review_integrity(db, pr_number=7, head_sha=head_a, n_reviewers=4, n_unparsed=0)
    head_b = origin.push_head(
        {"src/store.py": _FIXED, "api/tests/test_relabel.py": _MIGRATION_TEST},
        "fix: bind the prefix and test the relabel",
    )
    record_review_integrity(db, pr_number=7, head_sha=head_b, n_reviewers=4, n_unparsed=0)
    judge = _Judge()

    first = _gate(monkeypatch, origin, db, head_b, lambda: judge)

    rows = {f.id: f for f in fetch_findings(db, 7)}
    assert rows[security].status is FindingStatus.RESOLVED_AT_HEAD
    assert rows[missing].status is FindingStatus.RESOLVED_AT_HEAD
    assert rows[missing].verification_result == (
        f"resolved at {head_b[:7]}: test_relabels_generic_rows exists at api/tests/test_relabel.py:4"
    )
    assert first.facts.reverified == {"resolved": 2, "still_open": 0, "needs_judgement": 0}
    assert first.decision.reasons == []
    assert first.decision.merge is True
    assert judge.calls == 1

    second = _gate(monkeypatch, origin, db, head_b, _no_judge)

    assert second.decision.merge is True
    assert second.facts.open_blocking_findings == 0
