"""Tests for the release-gate CLI wiring (SP-RELEASE-GATE step 4).

Covers the ``ferova release gate`` and ``ferova release verify``
exit-code routing: 0 merge-ready / 5 refused / 1 could-not-evaluate,
mirroring the ``ferova review gate`` semantics.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import typer

from repoach.cli import release_cmds
from repoach.review.release_gate import (
    ReleaseDecision,
    ReleaseFacts,
    ReleaseVerifyResult,
    write_gate_receipt,
)


def _all_green_facts() -> ReleaseFacts:
    return ReleaseFacts(
        develop_sha="abc123",
        out_of_band_commits=[],
        remote_sha="abc123",
        pr_head_sha=None,
        ci_green=True,
    )


def test_cli_release_gate_exit_zero_when_merge_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_cmds,
        "gather_release_facts",
        lambda *, repo_root, gh, pr_number: _all_green_facts(),
    )
    release_cmds.release_gate(None)


def test_cli_release_gate_exit_five_when_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = ReleaseFacts(
        develop_sha="abc123",
        out_of_band_commits=[],
        remote_sha="abc123",
        pr_head_sha=None,
        ci_green=False,
    )
    monkeypatch.setattr(
        release_cmds,
        "gather_release_facts",
        lambda *, repo_root, gh, pr_number: facts,
    )
    with pytest.raises(typer.Exit) as exc:
        release_cmds.release_gate(None)
    assert exc.value.exit_code == 5


def test_cli_release_verify_exit_five_on_divergence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        release_cmds,
        "verify_release",
        lambda path, *, gh: ReleaseVerifyResult(
            verified=False,
            main_sha="def456",
            expected_sha="abc123",
            detail="main tip does not match the approved develop head",
        ),
    )
    with pytest.raises(typer.Exit) as exc:
        release_cmds.release_verify()
    assert exc.value.exit_code == 5


def _git(repo: Path, *args: str) -> str:
    """Run ``git`` in *repo* and return combined stdout+stderr stripped.

    Mirrors the throwaway-repo helper in ``tests/unit/test_release_gate.py``
    so the CLI regression test shares the same real-repo fixture style.
    """
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=False)
    return (proc.stdout + proc.stderr).strip()


def _init_origin_and_work(tmp_path: Path) -> Path:
    """Build a bare ``origin`` repo and a configured work clone, return the clone."""
    origin_dir = tmp_path / "origin.git"
    work_dir = tmp_path / "work"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin_dir))
    _git(tmp_path, "clone", "-q", str(origin_dir), str(work_dir))
    _git(work_dir, "config", "user.email", "test@example.invalid")
    _git(work_dir, "config", "user.name", "Test Runner")
    return work_dir


def _seed_main_and_develop(work_dir: Path) -> str:
    """Seed ``main`` with an init commit and ``develop`` with one feature commit.

    Returns the ``develop`` tip SHA after both branches are pushed.
    """
    (work_dir / "README.md").write_text("hello\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "chore: init")
    _git(work_dir, "push", "-q", "-u", "origin", "main")

    _git(work_dir, "switch", "-c", "develop")
    (work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(work_dir, "add", "-A")
    _git(work_dir, "commit", "-q", "-m", "Add feature (#1)")
    _git(work_dir, "push", "-q", "-u", "origin", "develop")
    return _git(work_dir, "rev-parse", "develop")


def _redirect_release_cmds(
    monkeypatch: pytest.MonkeyPatch, *, work_dir: Path, receipt_path: Path
) -> None:
    """Point ``release_cmds`` at a throwaway work repo and receipt path.

    Redirects only the module-level path constants so the real
    ``verify_release`` runs over a real ``GhCli`` against real git --
    no stubbing of ``verify_release`` itself.
    """
    monkeypatch.setattr(release_cmds, "_repo_root", lambda: work_dir)
    monkeypatch.setattr(release_cmds, "_RECEIPT_PATH", receipt_path)


def test_cli_release_verify_exit_codes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression guard: ``release_verify`` exits 0/5/1 over REAL git repos.

    G2 ("exited 0 despite verified: false") was a measurement artifact
    of piping through ``tail``; the CLI already exits 5 on refusal. This
    pins that contract without stubbing ``verify_release`` -- a real
    merge-commit repo verifies True (clean return), a real squash-shaped
    repo verifies False (``typer.Exit`` code 5), and a missing receipt is
    an evaluation error (``typer.Exit`` code 1).
    """
    merge_dir = tmp_path / "merge"
    merge_dir.mkdir()
    merge_work_dir = _init_origin_and_work(merge_dir)
    develop_sha = _seed_main_and_develop(merge_work_dir)
    _git(merge_work_dir, "switch", "main")
    _git(
        merge_work_dir,
        "merge",
        "--no-ff",
        "-q",
        "-m",
        "Merge develop into main",
        "develop",
    )
    _git(merge_work_dir, "push", "-q", "origin", "main")
    merge_receipt_path = tmp_path / "merge" / "release_gate_receipt.json"
    write_gate_receipt(
        merge_receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    _redirect_release_cmds(monkeypatch, work_dir=merge_work_dir, receipt_path=merge_receipt_path)
    release_cmds.release_verify()

    squash_dir = tmp_path / "squash"
    squash_dir.mkdir()
    squash_work_dir = _init_origin_and_work(squash_dir)
    develop_sha = _seed_main_and_develop(squash_work_dir)
    _git(squash_work_dir, "switch", "main")
    (squash_work_dir / "feature.txt").write_text("feature\n", encoding="utf-8")
    _git(squash_work_dir, "add", "-A")
    _git(squash_work_dir, "commit", "-q", "-m", "Add feature (#1) (squashed)")
    _git(squash_work_dir, "push", "-q", "origin", "main")
    squash_receipt_path = tmp_path / "squash" / "release_gate_receipt.json"
    write_gate_receipt(
        squash_receipt_path,
        develop_sha=develop_sha,
        decision=ReleaseDecision(merge=True, reasons=[]),
    )

    _redirect_release_cmds(monkeypatch, work_dir=squash_work_dir, receipt_path=squash_receipt_path)
    with pytest.raises(typer.Exit) as exc:
        release_cmds.release_verify()
    assert exc.value.exit_code == 5

    missing_receipt_path = tmp_path / "squash" / "no_such_receipt.json"
    _redirect_release_cmds(monkeypatch, work_dir=squash_work_dir, receipt_path=missing_receipt_path)
    with pytest.raises(typer.Exit) as exc:
        release_cmds.release_verify()
    assert exc.value.exit_code == 1
