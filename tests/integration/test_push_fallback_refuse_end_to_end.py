"""End-to-end integration test for SP-PUSH-FALLBACK-REFUSE.

Drives :func:`run_coder_fix_from_findings` against a real, hermetic
bare-origin + working-clone pair (no network beyond the local bare
repo). Only the ``gh`` boundary is faked — ``pr_view`` scripted to
return a PR with no ``headRefName`` — and only the CI/ruff/pytest
gates are stubbed (they are not the surface this spec changes). The
git plumbing itself, including ``git_commit_and_push``, runs for
real, so the assertion that nothing lands on the tmp repo's
``develop`` ref is ground truth, not a mocked expectation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

from repoach.review import coder_loop
from repoach.review.coder_findings import run_coder_fix_from_findings
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    init_findings_schema,
    record_finding,
)

_PR_NUMBER = 901


def _run_git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _rev_parse(cwd: Path, ref: str) -> str:
    result = subprocess.run(
        ["git", "rev-parse", ref], cwd=cwd, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


class _ScriptedGh:
    """Truthful boundary fake for ``gh``: only the PR-metadata API is scripted.

    Everything the fix under test actually exercises (the local git
    commit/push) is real; this fake stands in for the one seam that
    would otherwise require live network access to GitHub.
    """

    def __init__(self, *, head: str | None) -> None:
        self._head = head

    def pr_view(self, pr_number: int) -> dict[str, str]:
        del pr_number
        meta = {"baseRefName": "develop", "state": "OPEN"}
        if self._head is not None:
            meta["headRefName"] = self._head
        return meta

    def pr_head_sha(self, pr_number: int) -> str:
        del pr_number
        return "shaHEAD"

    def pr_diff(self, pr_number: int) -> SimpleNamespace:
        del pr_number
        return SimpleNamespace(ok=True, stdout="diff", stderr="")


class _FakeCoder:
    def __init__(self, plan: dict) -> None:
        self._plan = plan

    def respond_to_findings(self, **_kwargs) -> dict:
        return self._plan


def _seed_repo(tmp_path: Path) -> tuple[Path, Path]:
    origin = tmp_path / "origin.git"
    origin.mkdir()
    _run_git(["init", "--bare", "-q"], origin)

    work = tmp_path / "work"
    _run_git(["clone", "-q", str(origin), str(work)], tmp_path)
    _run_git(["checkout", "-q", "-b", "develop"], work)
    (work / "readme.txt").write_text("seed\n", encoding="utf-8")
    _run_git(["add", "readme.txt"], work)
    _run_git(
        ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "seed"],
        work,
    )
    _run_git(["push", "-q", "origin", "develop"], work)
    return origin, work


def _seed_one_finding(db: Path) -> None:
    init_findings_schema(db)
    record_finding(
        db,
        Finding(
            pr_number=_PR_NUMBER,
            head_sha="head123",
            round=1,
            finder="architect",
            claim_type=ClaimType.MISSING_TEST,
            severity=Severity.BLOCKING,
            file="tests/test_y.py",
            line_start=1,
            line_end=1,
            claim="missing test_resolved",
            evidence_pointer="tests/test_y.py:1",
            status=FindingStatus.VERIFIED,
        ),
    )


def _patch_non_push_gates(monkeypatch, work: Path) -> None:
    def _write_fix(*_a, **_k) -> tuple[int, list[str]]:
        (work / "tests").mkdir(exist_ok=True)
        (work / "tests" / "test_y.py").write_text(
            "def test_resolved():\n    assert True\n", encoding="utf-8"
        )
        return 1, []

    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_loop, "apply_fixes", _write_fix)
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "run_pytest_matrix", lambda *a, **k: (True, ""))


def test_push_refuses_when_no_head_branch_end_to_end(tmp_path: Path, monkeypatch) -> None:
    origin, work = _seed_repo(tmp_path)
    develop_before = _rev_parse(work, "develop")

    db = tmp_path / "f.db"
    _seed_one_finding(db)
    _patch_non_push_gates(monkeypatch, work)

    plan = {
        "fixes": [{"path": "tests/test_y.py", "new_content": "...", "rationale": "r"}],
        "commit_message": "fix(tests): add test",
        "summary": "added test_resolved",
    }
    res = run_coder_fix_from_findings(
        _PR_NUMBER,
        gh=_ScriptedGh(head=None),
        repo_root=work,
        coder=_FakeCoder(plan),
        db_path=db,
    )

    assert res.pushed is False
    assert res.no_op_reason is not None
    assert "head" in res.no_op_reason.lower()

    develop_after = subprocess.run(
        ["git", "rev-parse", "refs/heads/develop"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert develop_after == develop_before


def test_push_targets_real_head_branch_end_to_end(tmp_path: Path, monkeypatch) -> None:
    origin, work = _seed_repo(tmp_path)
    develop_before = _rev_parse(work, "develop")

    db = tmp_path / "f.db"
    _seed_one_finding(db)
    _patch_non_push_gates(monkeypatch, work)

    plan = {
        "fixes": [{"path": "tests/test_y.py", "new_content": "...", "rationale": "r"}],
        "commit_message": "fix(tests): add test",
        "summary": "added test_resolved",
    }
    res = run_coder_fix_from_findings(
        _PR_NUMBER,
        gh=_ScriptedGh(head="feat/x"),
        repo_root=work,
        coder=_FakeCoder(plan),
        db_path=db,
    )

    assert res.pushed is True

    feat_x_sha = subprocess.run(
        ["git", "rev-parse", "refs/heads/feat/x"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert feat_x_sha == _rev_parse(work, "HEAD")

    develop_after = subprocess.run(
        ["git", "rev-parse", "refs/heads/develop"],
        cwd=origin,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert develop_after == develop_before
