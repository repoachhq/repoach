"""Unit tests for SP-CODER-FINDINGS slice 8a — the resolution side.

Pins the to-fix queue selection, the ``verified -> open`` transition,
and the re-verify-at-head resolution for both mechanical and judged
claim types. The judge is injected (no live LLM); the re-verify
semantics inversion (a verifier confirms a *problem*; resolution means
it can no longer confirm it) is pinned explicitly.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import typer

from repoach.cli import review_cmds
from repoach.review import coder_findings, coder_loop
from repoach.review.coder_findings import (
    CoderFindingsResult,
    fetch_open_blocking_findings,
    open_verified_blocking,
    record_ci_failures_as_findings,
    resolve_broken_behavior_findings,
    reverify_resolution_for_pr,
    run_coder_fix_from_findings,
)
from repoach.review.findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from repoach.review.stuck import MAX_CODER_ROUNDS, fetch_coder_rounds, record_coder_round


def _finding(
    claim_type: ClaimType,
    *,
    status: FindingStatus = FindingStatus.VERIFIED,
    severity: Severity = Severity.BLOCKING,
    file: str = "src/m.py",
    claim: str = "smell",
) -> Finding:
    return Finding(
        pr_number=1,
        head_sha="head123",
        round=1,
        finder="architect",
        claim_type=claim_type,
        severity=severity,
        file=file,
        line_start=1,
        line_end=1,
        claim=claim,
        evidence_pointer=f"{file}:1",
        status=status,
    )


def _fixed_judge(reply: str):
    def _judge(_prompt: str) -> str:
        return reply

    return _judge


def test_fetch_open_blocking_selects_only_actionable(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.VERIFIED, claim="v"))
    record_finding(db, _finding(ClaimType.SECURITY, status=FindingStatus.OPEN, claim="o"))
    record_finding(
        db,
        _finding(
            ClaimType.DESIGN, status=FindingStatus.VERIFIED, severity=Severity.ADVISORY, claim="adv"
        ),
    )
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.REFUTED, claim="ref"))
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.RESOLVED, claim="res"))
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.PROPOSED, claim="prop"))

    open_blocking = fetch_open_blocking_findings(db, 1)
    claims = sorted(f.claim for f in open_blocking)
    assert claims == ["o", "v"]


def test_open_verified_blocking_moves_only_verified_blocking(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.VERIFIED, claim="blk"))
    record_finding(
        db,
        _finding(
            ClaimType.DESIGN, status=FindingStatus.VERIFIED, severity=Severity.ADVISORY, claim="adv"
        ),
    )

    moved = open_verified_blocking(db, 1, head_sha="head123")
    assert moved == 1
    statuses = {f.claim: f.status for f in fetch_findings(db, 1)}
    assert statuses["blk"] is FindingStatus.OPEN
    assert statuses["adv"] is FindingStatus.VERIFIED


def test_reverify_mechanical_resolves_when_fixed(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(
        "def test_present():\n    assert True\n", encoding="utf-8"
    )
    record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            status=FindingStatus.OPEN,
            file="tests/test_x.py",
            claim="missing test_present",
        ),
    )
    record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            status=FindingStatus.OPEN,
            file="tests/test_x.py",
            claim="missing test_absent_now",
        ),
    )

    counts = reverify_resolution_for_pr(db, pr_number=1, repo_root=tmp_path, head_sha="head456")
    assert counts == {"resolved": 1, "still_open": 1}
    by_claim = {f.claim: f.status for f in fetch_findings(db, 1)}
    assert by_claim["missing test_present"] is FindingStatus.RESOLVED
    assert by_claim["missing test_absent_now"] is FindingStatus.OPEN


def test_reverify_judged_resolves_on_refute(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.OPEN, claim="design smell"))

    counts = reverify_resolution_for_pr(
        db,
        pr_number=1,
        repo_root=tmp_path,
        head_sha="head456",
        judge_factory=lambda: _fixed_judge(
            'VERDICT: {"refuted": true, "reasoning": "fixed at head"}'
        ),
    )
    assert counts == {"resolved": 1, "still_open": 0}


def test_reverify_judged_stays_open_when_still_real(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "m.py").write_text("x = 1\n", encoding="utf-8")
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.OPEN, claim="design smell"))

    counts = reverify_resolution_for_pr(
        db,
        pr_number=1,
        repo_root=tmp_path,
        head_sha="head456",
        judge_factory=lambda: _fixed_judge(
            'VERDICT: {"refuted": false, "reasoning": "still present"}'
        ),
    )
    assert counts == {"resolved": 0, "still_open": 1}


class _FakeLoop:
    def __init__(self, text: str) -> None:
        self._text = text

    def run_oneshot(self, _prompt: str, *, json_response: bool = False) -> SimpleNamespace:
        return SimpleNamespace(text=self._text, model_used="fake", tokens_used=10, elapsed_s=0.1)


def test_respond_to_findings_parses_fix_plan(tmp_path: Path) -> None:
    from repoach.review.reviewer import Coder

    plan = {
        "fixes": [
            {"path": "src/m.py", "new_content": "x = 2\n", "rationale": "resolves finding 1"}
        ],
        "commit_message": "fix(m): resolve finding",
        "summary": "fixed",
    }
    coder = Coder(loop=_FakeLoop(json.dumps(plan)), logs_dir=tmp_path)
    out = coder.respond_to_findings(
        findings=[_finding(ClaimType.DESIGN, status=FindingStatus.OPEN, claim="smell")],
        diff="diff --git a/src/m.py b/src/m.py\n",
        pr_number=1,
    )
    assert out["fixes"] == [
        {"path": "src/m.py", "new_content": "x = 2\n", "rationale": "resolves finding 1"}
    ]
    assert out["commit_message"].startswith("fix(m)")


def test_respond_to_findings_parse_failure_is_graceful(tmp_path: Path) -> None:
    from repoach.review.reviewer import Coder

    coder = Coder(loop=_FakeLoop("not json at all"), logs_dir=tmp_path)
    out = coder.respond_to_findings(
        findings=[_finding(ClaimType.MISSING_TEST, status=FindingStatus.OPEN)],
        diff="",
        pr_number=2,
    )
    assert out["fixes"] == []
    assert out["summary"]


def _gh_mock(*, base: str = "develop", head: str = "feat/x") -> MagicMock:
    gh = MagicMock()
    gh.pr_view.return_value = {"baseRefName": base, "headRefName": head, "state": "OPEN"}
    gh.pr_head_sha.return_value = "head123"
    gh.pr_diff.return_value = SimpleNamespace(ok=True, stdout="diff", stderr="")
    return gh


class _FakeCoder:
    def __init__(self, plan: dict) -> None:
        self._plan = plan

    def respond_to_findings(self, **_kwargs) -> dict:
        return self._plan


def test_run_from_findings_refuses_non_develop_base(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(base="main"), repo_root=tmp_path, coder=_FakeCoder({}), db_path=db
    )
    assert res.pushed is False
    assert res.no_op_reason and "develop" in res.no_op_reason


def test_run_from_findings_noop_when_no_open_findings(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder({}), db_path=db
    )
    assert res.pushed is False
    assert res.no_op_reason == "no open blocking findings to resolve"


def test_run_from_findings_happy_path_resolves(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text(
        "def test_resolved():\n    assert True\n", encoding="utf-8"
    )
    record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            status=FindingStatus.VERIFIED,
            file="tests/test_y.py",
            claim="missing test_resolved",
        ),
    )

    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_loop, "apply_fixes", lambda *a, **k: (1, []))
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "run_pytest_matrix", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "git_commit_and_push", lambda **k: (True, "ok"))

    plan = {
        "fixes": [{"path": "tests/test_y.py", "new_content": "...", "rationale": "r"}],
        "commit_message": "fix(tests): add test",
        "summary": "added test_resolved",
    }
    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )
    assert res.pushed is True
    assert res.fixes_applied == 1
    assert res.resolved == 1
    assert res.still_open == 0
    by_claim = {f.claim: f.status for f in fetch_findings(db, 1)}
    assert by_claim["missing test_resolved"] is FindingStatus.RESOLVED


def _patch_push_path(monkeypatch) -> None:
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_loop, "apply_fixes", lambda *a, **k: (1, []))
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "run_pytest_matrix", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "git_commit_and_push", lambda **k: (True, "ok"))


def test_run_coder_fix_records_round_on_push(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_y.py").write_text(
        "def test_resolved():\n    assert True\n", encoding="utf-8"
    )
    record_finding(
        db,
        _finding(
            ClaimType.MISSING_TEST,
            status=FindingStatus.VERIFIED,
            file="tests/test_y.py",
            claim="missing test_resolved",
        ),
    )
    _patch_push_path(monkeypatch)
    plan = {"fixes": [{"path": "tests/test_y.py", "new_content": "...", "rationale": "r"}]}

    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )

    assert res.pushed is True
    rounds = fetch_coder_rounds(db, 1)
    assert len(rounds) == 1
    assert rounds[0].open_blocking_before == 1
    assert rounds[0].open_blocking_after == 0


def _settings_with_routine(db: Path) -> SimpleNamespace:
    return SimpleNamespace(
        db_path=str(db),
        claude_code_routine_id="rid",
        claude_code_routine_token=SimpleNamespace(get_secret_value=lambda: "tok"),
    )


def test_run_coder_fix_stuck_escalates_without_fixing(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(
        db, _finding(ClaimType.DESIGN, status=FindingStatus.VERIFIED, claim="cannot fix")
    )
    for _ in range(MAX_CODER_ROUNDS):
        record_coder_round(db, pr_number=1, open_blocking_before=1, open_blocking_after=1)
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_findings, "get_settings", lambda: _settings_with_routine(db))
    fired: dict[str, object] = {}

    def _fake_fire(*, routine_id: str, token: str, payload: dict) -> SimpleNamespace:
        fired["payload"] = payload
        return SimpleNamespace(
            ok=True, status_code=200, session_id="s", session_url="u", error=None
        )

    coder = MagicMock()
    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=coder, db_path=db, routine_fire=_fake_fire
    )

    assert res.stuck is True
    assert res.no_op_reason and res.no_op_reason.startswith("stuck —")
    coder.respond_to_findings.assert_not_called()
    assert fired["payload"]["kind"] == "stuck_escalation"
    assert len(fetch_findings(db, 1, status=FindingStatus.STUCK)) == 1


def test_run_coder_fix_stuck_skips_when_no_open(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    for _ in range(MAX_CODER_ROUNDS):
        record_coder_round(db, pr_number=1, open_blocking_before=1, open_blocking_after=1)
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))

    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=MagicMock(), db_path=db
    )

    assert res.stuck is False
    assert res.no_op_reason == "no open blocking findings to resolve"


def test_record_ci_failures_materializes_and_is_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    rows = [
        {"name": "Test suite (Python 3.11)", "link": "https://x/runs/1/job/2"},
        {"name": "Test suite (Python 3.13)", "link": "https://x/runs/1/job/3"},
    ]
    logs = ["### Test suite (Python 3.11)\nFAILED test_a", "### Test suite (Python 3.13)\nok"]
    created = record_ci_failures_as_findings(
        db, pr_number=1, head_sha="h1", failed_rows=rows, failure_logs=logs
    )
    assert created == 2
    bb = [f for f in fetch_findings(db, 1) if f.claim_type is ClaimType.BROKEN_BEHAVIOR]
    assert len(bb) == 2
    assert all(f.status is FindingStatus.VERIFIED and f.severity is Severity.BLOCKING for f in bb)
    again = record_ci_failures_as_findings(db, pr_number=1, head_sha="h1", failed_rows=rows)
    assert again == 0
    assert len([f for f in fetch_findings(db, 1) if f.claim_type is ClaimType.BROKEN_BEHAVIOR]) == 2


def test_resolve_broken_behavior_when_green(tmp_path: Path) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(
        db,
        _finding(
            ClaimType.BROKEN_BEHAVIOR,
            status=FindingStatus.OPEN,
            file="(ci):Test suite",
            claim="CI check failed: Test suite",
        ),
    )
    resolved = resolve_broken_behavior_findings(db, pr_number=1, head_sha="h2")
    assert resolved == 1
    bb = next(f for f in fetch_findings(db, 1) if f.claim_type is ClaimType.BROKEN_BEHAVIOR)
    assert bb.status is FindingStatus.RESOLVED


def test_run_from_findings_materializes_ci_red(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    init_findings_schema(db)

    monkeypatch.setattr(
        coder_loop,
        "fetch_ci_status",
        lambda *a, **k: (
            coder_loop.CI_RED,
            [{"name": "Test suite", "link": "https://x/runs/1/job/2"}],
        ),
    )
    monkeypatch.setattr(
        coder_loop, "fetch_failed_check_logs", lambda *a, **k: ["### Test suite\nFAILED"]
    )
    monkeypatch.setattr(coder_loop, "apply_fixes", lambda *a, **k: (1, []))
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "run_pytest_matrix", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "git_commit_and_push", lambda **k: (True, "ok"))

    plan = {
        "fixes": [{"path": "src/m.py", "new_content": "x = 2\n", "rationale": "r"}],
        "commit_message": "fix: ci",
        "summary": "fixed ci",
    }
    res = run_coder_fix_from_findings(
        7, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )
    assert res.pushed is True
    assert res.n_open_findings == 1
    assert res.resolved == 1
    bb = next(f for f in fetch_findings(db, 7) if f.claim_type is ClaimType.BROKEN_BEHAVIOR)
    assert bb.status is FindingStatus.RESOLVED


def test_run_from_findings_resolves_stale_ci_finding_when_ci_turns_green(
    tmp_path: Path, monkeypatch
) -> None:
    """A broken_behavior finding left OPEN from a prior round is resolved
    when CI is already green at entry, even when the Coder has no push to make."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    record_finding(
        db,
        _finding(
            ClaimType.BROKEN_BEHAVIOR,
            status=FindingStatus.OPEN,
            file="(ci):Test suite",
            claim="CI check failed: Test suite",
        ),
    )
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    res = run_coder_fix_from_findings(1, gh=_gh_mock(), repo_root=tmp_path, db_path=db)
    assert res.pushed is False
    assert res.no_op_reason == "no open blocking findings to resolve"
    bb = next(f for f in fetch_findings(db, 1) if f.claim_type is ClaimType.BROKEN_BEHAVIOR)
    assert bb.status is FindingStatus.RESOLVED


def _seed_one_open_blocker(db: Path) -> None:
    init_findings_schema(db)
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.VERIFIED, claim="smell"))


def test_placeholder_rejection_flags_and_persists(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    _seed_one_open_blocker(db)
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))

    placeholder = {"path": "src/m.py", "reason": "placeholder", "evidence": "# ... rest ..."}

    def _apply_placeholder(*_a, placeholder_rejections_out=None, **_k):
        if placeholder_rejections_out is not None:
            placeholder_rejections_out.append(placeholder)
        return (0, ["src/m.py"])

    monkeypatch.setattr(coder_loop, "apply_fixes", _apply_placeholder)
    persist_mock = MagicMock(return_value=tmp_path / "coder_placeholder_rejected_1.txt")
    monkeypatch.setattr(coder_loop, "persist_placeholder_rejected", persist_mock)

    plan = {
        "fixes": [{"path": "src/m.py", "new_content": "# ... rest ...", "rationale": "r"}],
        "commit_message": "c",
        "summary": "s",
    }
    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )

    assert res.pushed is False
    assert res.placeholder_rejected is True
    persist_mock.assert_called_once()
    assert persist_mock.call_args.kwargs["rejected"] == [placeholder]


def test_whitelist_only_rejection_not_flagged(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "f.db"
    _seed_one_open_blocker(db)
    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_loop, "apply_fixes", lambda *a, **k: (0, [".github/workflows/x.yml"]))
    persist_mock = MagicMock()
    monkeypatch.setattr(coder_loop, "persist_placeholder_rejected", persist_mock)

    plan = {
        "fixes": [{"path": ".github/workflows/x.yml", "new_content": "x", "rationale": "r"}],
        "commit_message": "c",
        "summary": "s",
    }
    res = run_coder_fix_from_findings(
        2, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )

    assert res.pushed is False
    assert res.placeholder_rejected is False
    persist_mock.assert_not_called()


def test_cli_from_findings_placeholder_exits_9(monkeypatch) -> None:
    monkeypatch.setattr(
        review_cmds,
        "run_coder_fix_from_findings",
        lambda pr: CoderFindingsResult(pr_number=pr, placeholder_rejected=True, pushed=False),
    )
    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_fix(5)
    assert exc.value.exit_code == 9


def test_cli_from_findings_whitelist_exits_4(monkeypatch) -> None:
    monkeypatch.setattr(
        review_cmds,
        "run_coder_fix_from_findings",
        lambda pr: CoderFindingsResult(pr_number=pr, placeholder_rejected=False, pushed=False),
    )
    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_fix(6)
    assert exc.value.exit_code == 4


def test_cli_from_findings_stuck_exits_3(monkeypatch) -> None:
    monkeypatch.setattr(
        review_cmds,
        "run_coder_fix_from_findings",
        lambda pr: CoderFindingsResult(pr_number=pr, stuck=True, pushed=False),
    )
    with pytest.raises(typer.Exit) as exc:
        review_cmds.review_fix(7)
    assert exc.value.exit_code == 3


def _seed_findings_with_writing_apply(
    tmp_path: Path, monkeypatch, marker: Path
) -> tuple[Path, dict]:
    """Seed an open blocker and an ``apply_fixes`` that writes *marker* on disk."""
    db = tmp_path / "f.db"
    init_findings_schema(db)
    (tmp_path / "src").mkdir()
    record_finding(db, _finding(ClaimType.DESIGN, status=FindingStatus.VERIFIED, claim="smell"))

    def _apply(*_a, **_k) -> tuple[int, list]:
        marker.write_text("x = 1\n", encoding="utf-8")
        return 1, []

    monkeypatch.setattr(coder_loop, "fetch_ci_status", lambda *a, **k: (coder_loop.CI_GREEN, []))
    monkeypatch.setattr(coder_loop, "apply_fixes", _apply)
    plan = {"fixes": [{"path": "src/m.py", "new_content": "x = 1\n", "rationale": "r"}]}
    return db, plan


def test_run_from_findings_ruff_red_leaves_work_on_disk(tmp_path: Path, monkeypatch) -> None:
    """SP-DEVAGENT-WIRE: a red ruff gate no-ops without a destructive revert."""
    marker = tmp_path / "src" / "applied.py"
    db, plan = _seed_findings_with_writing_apply(tmp_path, monkeypatch, marker)
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (False, "E501 too long"))

    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )

    assert res.pushed is False
    assert res.no_op_reason and "ruff" in res.no_op_reason
    assert "revert" not in res.no_op_reason.lower()
    assert marker.read_text(encoding="utf-8") == "x = 1\n"


def test_run_from_findings_pytest_red_leaves_work_on_disk(tmp_path: Path, monkeypatch) -> None:
    """SP-DEVAGENT-WIRE: a red pytest gate no-ops without a destructive revert."""
    marker = tmp_path / "src" / "applied.py"
    db, plan = _seed_findings_with_writing_apply(tmp_path, monkeypatch, marker)
    monkeypatch.setattr(coder_loop, "run_ruff_gate", lambda *a, **k: (True, ""))
    monkeypatch.setattr(coder_loop, "run_pytest_matrix", lambda *a, **k: (False, "1 failed"))

    res = run_coder_fix_from_findings(
        1, gh=_gh_mock(), repo_root=tmp_path, coder=_FakeCoder(plan), db_path=db
    )

    assert res.pushed is False
    assert res.pytest_passed is False
    assert res.no_op_reason and "pytest" in res.no_op_reason
    assert "revert" not in res.no_op_reason.lower()
    assert marker.read_text(encoding="utf-8") == "x = 1\n"


def test_revert_working_tree_removed_from_coder_loop() -> None:
    """The destructive revert is gone entirely (SP-DEVAGENT-WIRE)."""
    assert not hasattr(coder_loop, "revert_working_tree")


def test_run_from_findings_still_calls_ci_materialiser_and_resolver() -> None:
    """SP-CI-FINDINGS-WIRE AC5 regression guard.

    The CI materialiser (``record_ci_failures_as_findings``) was
    implemented with zero callers and nobody noticed. This static
    source-level assertion fails immediately and loudly if either
    ``record_ci_failures_as_findings`` or
    ``resolve_broken_behavior_findings`` is ever deleted from the
    coder-loop entry path again, independent of whether other
    behavioural tests around it are weakened or refactored at the
    same time.
    """
    source = inspect.getsource(run_coder_fix_from_findings)
    assert "record_ci_failures_as_findings" in source
    assert "resolve_broken_behavior_findings" in source
