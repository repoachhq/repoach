"""Unit tests for SP-RELEASE-GATE -- the pure release-range classifier and decision core.

The subject-only classifier is pinned on the squash-subject suffix
pattern (weaker fallback); the ledger-backed classifier
(SP-RELEASE-PROVENANCE-LEDGER) is pinned on SHA membership in the
recorded ``pr_merges`` merges regardless of subject text; the decision
function is pinned on every red-fact condition, including the
``provenance_error`` fail-closed reason.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.gh_client import GhCli, GhResult
from repoach.review.persistence import init_schema, record_merge
from repoach.review.release_gate import (
    ReleaseDecision,
    ReleaseFacts,
    classify_release_range,
    classify_release_range_against_ledger,
    compute_release_decision,
    gather_release_facts,
    verify_release,
    write_gate_receipt,
)


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in *repo* and return combined stdout+stderr stripped.

    Mirrors the helper in ``tests/integration/test_release_gate_end_to_end.py``
    so the throwaway-repo tests share one fixture style.
    """
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _facts(**over: object) -> ReleaseFacts:
    base: dict[str, object] = {
        "develop_sha": "abc123",
        "out_of_band_commits": [],
        "remote_sha": "abc123",
        "pr_head_sha": None,
        "ci_green": True,
        "ci_checked": True,
    }
    base.update(over)
    return ReleaseFacts(**base)


def test_release_range_all_squashes() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Fix sentinel max tokens (#37)",
        "Wire release gate CLI (#59)",
    ]
    assert classify_release_range(subjects) == []


def test_release_range_flags_non_pr_commit() -> None:
    subjects = [
        "Add retry budget knob (#12)",
        "Hotfix: patch prod config directly",
        "Wire release gate CLI (#59)",
    ]
    flagged = classify_release_range(subjects)
    assert flagged == ["Hotfix: patch prod config directly"]


def test_forged_pr_suffix_without_ledger_record_is_out_of_band() -> None:
    commits = [
        ("sha-legit-1", "Add retry budget knob (#12)"),
        ("sha-legit-2", "Wire release gate CLI (#59)"),
        ("sha-forged", "hotfix something (#99)"),
    ]
    merged_shas = {"sha-legit-1", "sha-legit-2"}

    flagged = classify_release_range_against_ledger(commits, merged_shas)

    assert flagged == ["hotfix something (#99)"]


def test_ledger_backed_commit_accepted_without_pr_suffix() -> None:
    commits = [("sha-legit", "Add retry budget knob")]
    merged_shas = {"sha-legit"}

    assert classify_release_range_against_ledger(commits, merged_shas) == []


def _gh_stub_with_commits(
    *,
    develop_sha: str,
    remote_sha: str,
    commits: list[tuple[str, str]],
    pr_head_sha: str | None = None,
) -> MagicMock:
    gh = MagicMock()
    gh.pr_head_sha.return_value = pr_head_sha

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:2] == ["rev-parse", "develop"]:
            return GhResult(returncode=0, stdout=f"{develop_sha}\n", stderr="", argv=args)
        if args[:1] == ["log"]:
            stdout = "".join(f"{sha}\t{subject}\n" for sha, subject in commits)
            return GhResult(returncode=0, stdout=stdout, stderr="", argv=args)
        if args[:2] == ["ls-remote", "origin"]:
            stdout = f"{remote_sha}\trefs/heads/develop\n" if remote_sha else ""
            return GhResult(returncode=0, stdout=stdout, stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side
    return gh


def _noop_ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], 0)


def test_gather_release_facts_flags_forged_pr_suffix_via_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "review.sqlite"
    init_schema(db_path)
    record_merge(
        db_path,
        pr_number=12,
        outcome="APPROVE",
        base_ref="develop",
        head_ref="feat/knob",
        merged_sha="sha-legit-1",
        notes="",
    )
    commits = [
        ("sha-legit-1", "Add retry budget knob (#12)"),
        ("sha-forged", "hotfix something (#99)"),
    ]
    gh = _gh_stub_with_commits(develop_sha="sha-forged", remote_sha="sha-forged", commits=commits)

    facts = gather_release_facts(
        repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner, db_path=db_path
    )
    decision = compute_release_decision(facts)

    assert facts.out_of_band_commits == ["hotfix something (#99)"]
    assert decision.merge is False
    assert any("hotfix something (#99)" in reason for reason in decision.reasons)


def test_gather_release_facts_accepts_clean_ledger_backed_range(tmp_path: Path) -> None:
    db_path = tmp_path / "review.sqlite"
    init_schema(db_path)
    record_merge(
        db_path,
        pr_number=12,
        outcome="APPROVE",
        base_ref="develop",
        head_ref="feat/knob",
        merged_sha="sha-legit-1",
        notes="",
    )
    commits = [("sha-legit-1", "Add retry budget knob (#12)")]
    gh = _gh_stub_with_commits(develop_sha="sha-legit-1", remote_sha="sha-legit-1", commits=commits)

    facts = gather_release_facts(
        repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner, db_path=db_path
    )
    decision = compute_release_decision(facts)

    assert facts.out_of_band_commits == []
    assert facts.provenance_error is None
    assert decision.merge is True
    assert decision.reasons == []


def test_gather_release_facts_fails_closed_on_empty_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "review.sqlite"
    commits = [("sha-1", "Add retry budget knob (#12)")]
    gh = _gh_stub_with_commits(develop_sha="sha-1", remote_sha="sha-1", commits=commits)

    facts = gather_release_facts(
        repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner, db_path=db_path
    )
    decision = compute_release_decision(facts)

    assert facts.provenance_error is not None
    assert facts.out_of_band_commits == []
    assert decision.merge is False
    assert any("provenance unverifiable" in reason for reason in decision.reasons)


def test_gather_release_facts_fails_closed_on_unreadable_ledger(tmp_path: Path) -> None:
    db_path = tmp_path / "not_a_sqlite_file"
    db_path.mkdir()
    commits = [("sha-1", "Add retry budget knob (#12)")]
    gh = _gh_stub_with_commits(develop_sha="sha-1", remote_sha="sha-1", commits=commits)

    facts = gather_release_facts(
        repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner, db_path=db_path
    )
    decision = compute_release_decision(facts)

    assert facts.provenance_error is not None
    assert decision.merge is False
    assert any("provenance unverifiable" in reason for reason in decision.reasons)


def test_gather_release_facts_without_db_path_falls_back_to_subject_regex(
    tmp_path: Path,
) -> None:
    commits = [
        ("sha-1", "Add retry budget knob (#12)"),
        ("sha-2", "Hotfix: patch prod config directly"),
    ]
    gh = _gh_stub_with_commits(develop_sha="sha-2", remote_sha="sha-2", commits=commits)

    facts = gather_release_facts(repo_root=tmp_path, gh=gh, ci_runner=_noop_ci_runner)

    assert facts.provenance_error is None
    assert facts.out_of_band_commits == ["Hotfix: patch prod config directly"]


def test_release_gate_fail_closed_on_red_ci() -> None:
    facts = _facts(ci_green=False)
    decision = compute_release_decision(facts)
    assert decision.merge is False
    assert any("CI" in reason for reason in decision.reasons)


def _gh_stub(*, develop_sha: str, remote_sha: str, pr_head_sha: str | None = None) -> MagicMock:
    gh = MagicMock()
    gh.pr_head_sha.return_value = pr_head_sha

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:2] == ["rev-parse", "develop"]:
            return GhResult(returncode=0, stdout=f"{develop_sha}\n", stderr="", argv=args)
        if args[:1] == ["log"]:
            return GhResult(returncode=0, stdout="", stderr="", argv=args)
        if args[:2] == ["ls-remote", "origin"]:
            stdout = f"{remote_sha}\trefs/heads/develop\n" if remote_sha else ""
            return GhResult(returncode=0, stdout=stdout, stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side
    return gh


def test_release_gate_refuses_stale_head(tmp_path: Path) -> None:
    gh = _gh_stub(develop_sha="abc123", remote_sha="def456")

    def _ci_runner(repo_root: Path) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess([], 0)

    facts = gather_release_facts(repo_root=tmp_path, gh=gh, ci_runner=_ci_runner)
    decision = compute_release_decision(facts)
    assert decision.merge is False
    assert any("abc123" in reason and "def456" in reason for reason in decision.reasons)


def test_release_gate_missing_ci_script_is_error(tmp_path: Path) -> None:
    gh = _gh_stub(develop_sha="a", remote_sha="a")
    with pytest.raises(FileNotFoundError):
        gather_release_facts(repo_root=tmp_path, gh=gh)


def test_release_verify_detects_squash_divergence(tmp_path: Path) -> None:
    receipt_path = tmp_path / "release_gate_receipt.json"
    decision = ReleaseDecision(merge=True, reasons=[])
    write_gate_receipt(receipt_path, develop_sha="deadbeef", decision=decision)

    gh = MagicMock()

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:2] == ["ls-remote", "origin"]:
            return GhResult(
                returncode=0,
                stdout="cafef00d\trefs/heads/main\n",
                stderr="",
                argv=args,
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side

    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False
    assert "squash" in result.detail or "stale" in result.detail


def test_release_gate_never_calls_merge() -> None:
    source = Path(__file__).resolve().parents[2] / "src" / "repoach" / "review" / "release_gate.py"
    text = source.read_text(encoding="utf-8")
    assert "pr merge" not in text
    assert "gh pr merge" not in text
    assert "git push" not in text


def _init_origin_and_work(tmp_path: Path) -> tuple[Path, Path]:
    """Build a bare ``origin`` repo and a configured work clone.

    Mirrors the fixture in ``tests/integration/test_release_gate_end_to_end.py``:
    a bare ``origin`` with ``main`` as the default branch, cloned into a
    working tree with a throwaway commit identity configured.
    """
    origin_dir = tmp_path / "origin.git"
    work_dir = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(work_dir))
    _git(work_dir, "config", "user.email", "test@example.invalid")
    _git(work_dir, "config", "user.name", "Test Runner")
    return origin_dir, work_dir


def test_verify_accepts_merge_commit_release(tmp_path: Path) -> None:
    """A sanctioned ``git merge --no-ff`` of develop into main verifies True.

    Builds a real throwaway origin/work pair, seeds ``main`` with an
    initial commit, branches ``develop`` with one extra commit, then
    performs the exact merge shape ``release gate`` prescribes --
    "Create a merge commit" -- and pushes. The gate receipt records the
    develop tip (as :func:`write_gate_receipt` does in real operation).
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    _git(work_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(work_dir, "push", "-q", "origin", "main")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is True


def test_verify_still_refuses_squash(tmp_path: Path) -> None:
    """A squash-style advance of main (not a merge of develop) verifies False.

    Main advances by a plain commit that is neither the approved SHA
    nor a merge commit whose second parent is the approved SHA -- the
    shape a squash merge (or an unrelated commit) produces.
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1) (squashed)")
    _git(work_dir, "push", "-q", "origin", "main")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False
    assert "squash" in result.detail or "stale" in result.detail


def test_verify_refuses_stale_merge(tmp_path: Path) -> None:
    """A sanctioned merge commit taken, then develop advances -- distance != 0.

    After ``git merge --no-ff`` lands the approved develop head on
    main, develop moves again before verify runs, so
    ``main..develop`` is no longer zero and the merge is stale.
    """
    _origin_dir, work_dir = _init_origin_and_work(tmp_path)

    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(work_dir, "rev-parse", "develop")

    _git(work_dir, "switch", "main")
    _git(work_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(work_dir, "push", "-q", "origin", "main")

    _git(work_dir, "switch", "develop")
    (work_dir / "feature2.txt").write_text("feature 2\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature 2 (#2)")
    _git(work_dir, "push", "-q", "origin", "develop")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=work_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is False


def test_verify_release_fetches_before_rev_parse_checks(tmp_path: Path) -> None:
    """``verify_release`` fetches ``origin main develop`` before the stale-ref reads.

    A recording ``MagicMock`` stands in for ``gh._run_git``: every call
    is appended to a list in invocation order, then the fetch call's
    index is asserted to precede both the ``rev-parse origin/main^2``
    and ``rev-list origin/main..origin/develop`` calls.
    """
    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha="deadbeef",
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    calls: list[list[str]] = []

    gh = MagicMock()

    def _run_git_side(args: list[str]) -> GhResult:
        calls.append(args)
        if args[:2] == ["ls-remote", "origin"]:
            return GhResult(
                returncode=0, stdout="deadbeef\trefs/heads/main\n", stderr="", argv=args
            )
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side

    verify_release(receipt_path, gh=gh)

    fetch_index = calls.index(["fetch", "--quiet", "origin", "main", "develop"])
    rev_parse_index = calls.index(["rev-parse", "origin/main^2"])
    rev_list_index = calls.index(["rev-list", "--count", "origin/main..origin/develop"])
    assert fetch_index < rev_parse_index
    assert fetch_index < rev_list_index


def test_verify_release_raises_on_fetch_failure(tmp_path: Path) -> None:
    """A failing ``git fetch`` raises rather than falling through to stale-ref reads."""
    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha="deadbeef",
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = MagicMock()

    def _run_git_side(args: list[str]) -> GhResult:
        if args[:1] == ["fetch"]:
            return GhResult(returncode=1, stdout="", stderr="fatal: unable to access\n", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    gh._run_git.side_effect = _run_git_side

    with pytest.raises(RuntimeError):
        verify_release(receipt_path, gh=gh)


def test_verify_release_fetches_stale_local_refs_before_merge_check(tmp_path: Path) -> None:
    """A sanctioned merge is correctly reported even from a clone with stale local refs.

    Builds one bare ``origin`` and two independent work clones,
    "developer" and "operator". "operator" clones ``origin`` before the
    release merge happens, so its local ``origin/main`` remote-tracking
    ref is stale (pre-merge). "developer" then performs the exact
    sanctioned ``git merge --no-ff`` shape onto ``main`` and pushes.
    Without running any fetch in "operator", ``verify_release`` is
    called directly against it: on pre-change code (no internal fetch)
    this reproduces the live bug -- ``result.verified`` comes back
    False even though the live ``main`` state is a valid sanctioned
    merge. Once ``verify_release`` fetches first, it correctly reports
    True.
    """
    origin_dir = tmp_path / "origin.git"
    developer_dir = tmp_path / "developer"
    operator_dir = tmp_path / "operator"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(developer_dir))
    _git(developer_dir, "config", "user.email", "test@example.invalid")
    _git(developer_dir, "config", "user.name", "Test Runner")

    (developer_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(developer_dir, "add", "-A")
    _git(developer_dir, "commit", "-q", "-m", "chore: init")
    _git(developer_dir, "push", "-q", "-u", "origin", "main")

    _git(tmp_path, "clone", "-q", str(origin_dir), str(operator_dir))
    _git(operator_dir, "config", "user.email", "test@example.invalid")
    _git(operator_dir, "config", "user.name", "Test Runner")

    _git(developer_dir, "switch", "-c", "develop")
    (developer_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(developer_dir, "add", "-A")
    _git(developer_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(developer_dir, "push", "-q", "-u", "origin", "develop")
    develop_sha = _git(developer_dir, "rev-parse", "develop")

    _git(developer_dir, "switch", "main")
    _git(developer_dir, "merge", "--no-ff", "-q", "-m", "Merge develop into main", "develop")
    _git(developer_dir, "push", "-q", "origin", "main")

    receipt_path = tmp_path / "release_gate_receipt.json"
    write_gate_receipt(
        receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    gh = GhCli(cwd=operator_dir)
    result = verify_release(receipt_path, gh=gh)

    assert result.verified is True
