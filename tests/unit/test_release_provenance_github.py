"""Unit tests for SP-RELEASE-PROVENANCE-GH-FALLBACK.

Covers the opt-in GitHub-verified release-range provenance source: the
CLI default stays ``ledger`` (AC1), the ``github`` source classifies a
range by ``mergeCommitOid`` membership through the same pure classifier
the ledger source uses (AC2), an empty GitHub merged-set fails closed
exactly like an empty ledger (AC3), and the new
``GhCli.merged_pr_merge_shas`` method never issues a merge or push
(AC5).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from repoach.cli import release_cmds
from repoach.cli.release_cmds import release_app
from repoach.review.gh_client import GhCli, GhResult
from repoach.review.release_gate import (
    ProvenanceSource,
    ReleaseFacts,
    compute_release_decision,
    gather_release_facts,
)


def _gh_stub_with_commits(
    *,
    develop_sha: str,
    remote_sha: str,
    commits: list[tuple[str, str]],
    merged_shas: set[str],
    pr_head_sha: str | None = None,
) -> MagicMock:
    """Build a ``MagicMock`` ``GhCli`` stand-in with a fixed GitHub merged-SHA set.

    Mirrors ``_gh_stub_with_commits`` in ``tests/unit/test_release_gate.py``
    (the ledger-provenance sibling), adding
    ``merged_pr_merge_shas.return_value`` for the new GitHub source.
    """
    gh = MagicMock()
    gh.pr_head_sha.return_value = pr_head_sha
    gh.merged_pr_merge_shas.return_value = merged_shas

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


def test_release_gate_provenance_option_defaults_to_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC1: no flag / ``--provenance ledger`` reaches ``gather_release_facts`` as LEDGER.

    Drives the real Typer CLI parsing (``CliRunner``) so the default
    Click renders for an un-supplied ``--provenance`` option is exactly
    what a real invocation would bind -- ``gather_release_facts`` is
    faked to capture the keyword arguments it actually received.
    """
    captured: dict[str, object] = {}

    def _fake_gather_release_facts(
        *,
        repo_root: Path,
        gh: GhCli,
        pr_number: int | None,
        db_path: Path,
        provenance: ProvenanceSource,
    ) -> ReleaseFacts:
        captured["provenance"] = provenance
        captured["db_path"] = db_path
        return ReleaseFacts(
            develop_sha="abc123",
            out_of_band_commits=[],
            remote_sha="abc123",
            pr_head_sha=None,
            ci_green=True,
        )

    monkeypatch.setattr(release_cmds, "gather_release_facts", _fake_gather_release_facts)

    runner = CliRunner()
    result = runner.invoke(release_app, ["gate"])

    assert result.exit_code == 0, result.output
    assert captured["provenance"] is ProvenanceSource.LEDGER
    assert captured["db_path"] is not None


def test_github_provenance_flags_only_unmerged_shas(tmp_path: Path) -> None:
    """AC2: a commit whose SHA is absent from the GitHub merged-set is flagged.

    Every other commit's SHA is present in the fake ``gh``'s
    ``merged_pr_merge_shas`` set, so only the truly out-of-band commit
    appears in ``out_of_band_commits`` and ``provenance_error`` is
    ``None``.
    """
    commits = [
        ("sha-covered-1", "Add retry budget knob"),
        ("sha-covered-2", "Wire release gate CLI"),
        ("sha-missing", "hotfix something"),
    ]
    merged_shas = {"sha-covered-1", "sha-covered-2"}
    gh = _gh_stub_with_commits(
        develop_sha="sha-missing",
        remote_sha="sha-missing",
        commits=commits,
        merged_shas=merged_shas,
    )

    facts = gather_release_facts(
        repo_root=tmp_path,
        gh=gh,
        ci_runner=_noop_ci_runner,
        provenance=ProvenanceSource.GITHUB,
    )

    assert facts.out_of_band_commits == ["hotfix something"]
    assert facts.provenance_error is None
    gh.merged_pr_merge_shas.assert_called_once_with(base="develop")


def test_github_provenance_all_covered_yields_clean_merge_decision(tmp_path: Path) -> None:
    """AC2: every range commit covered by the GitHub set yields a clean, mergeable decision."""
    commits = [("sha-covered-1", "Add retry budget knob")]
    gh = _gh_stub_with_commits(
        develop_sha="sha-covered-1",
        remote_sha="sha-covered-1",
        commits=commits,
        merged_shas={"sha-covered-1"},
    )

    facts = gather_release_facts(
        repo_root=tmp_path,
        gh=gh,
        ci_runner=_noop_ci_runner,
        provenance=ProvenanceSource.GITHUB,
    )
    decision = compute_release_decision(facts)

    assert facts.out_of_band_commits == []
    assert facts.provenance_error is None
    assert decision.merge is True
    assert decision.reasons == []


def test_github_provenance_empty_set_refuses(tmp_path: Path) -> None:
    """AC3: an empty GitHub merged-set with a non-empty range fails closed."""
    commits = [("sha-1", "Add retry budget knob")]
    gh = _gh_stub_with_commits(
        develop_sha="sha-1",
        remote_sha="sha-1",
        commits=commits,
        merged_shas=set(),
    )

    facts = gather_release_facts(
        repo_root=tmp_path,
        gh=gh,
        ci_runner=_noop_ci_runner,
        provenance=ProvenanceSource.GITHUB,
    )
    decision = compute_release_decision(facts)

    assert facts.provenance_error is not None
    assert facts.out_of_band_commits == []
    assert decision.merge is False
    assert any("provenance unverifiable" in reason for reason in decision.reasons)


class _RecordingGhCli(GhCli):
    """``GhCli`` that records every argv and returns a canned merged-PR list.

    Mirrors the ``_CannedGhCli`` / ``_PagedUpsertGhCli`` boundary-fake
    style in ``tests/unit/test_gh_client.py`` -- only ``_run`` (the
    subprocess boundary) is faked, so the real ``merged_pr_merge_shas``
    parsing logic runs unchanged.
    """

    def __init__(self, *, merge_commit_oids: list[str]) -> None:
        super().__init__()
        self._merge_commit_oids = merge_commit_oids
        self.calls: list[list[str]] = []

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        self.calls.append(args)
        payload = [{"mergeCommitOid": sha} for sha in self._merge_commit_oids]
        return GhResult(returncode=0, stdout=json.dumps(payload), stderr="", argv=args)


def test_merged_pr_merge_shas_is_read_only() -> None:
    """AC5: ``merged_pr_merge_shas`` issues only ``gh pr list``, never merge/push."""
    cli = _RecordingGhCli(merge_commit_oids=["sha-a", "sha-b", ""])

    shas = cli.merged_pr_merge_shas(base="develop")

    assert shas == {"sha-a", "sha-b"}
    assert len(cli.calls) == 1
    issued = cli.calls[0]
    assert issued[:2] == ["pr", "list"]
    assert "merge" not in issued
    assert "push" not in issued
