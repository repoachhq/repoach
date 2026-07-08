"""Tests for the release-gate CLI wiring (SP-RELEASE-GATE step 4).

Covers the ``ferova release gate`` and ``ferova release verify``
exit-code routing: 0 merge-ready / 5 refused / 1 could-not-evaluate,
mirroring the ``ferova review gate`` semantics.
"""

from __future__ import annotations

import pytest
import typer

from ferova.cli import release_cmds
from ferova.review.release_gate import ReleaseFacts, ReleaseVerifyResult


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
