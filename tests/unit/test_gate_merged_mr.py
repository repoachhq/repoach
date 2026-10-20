"""``review gate`` on a merged MR, and a deleted branch named as such.

SP-GATE-MERGED-MR-NAMED-REASON. Measured 2026-10-20 on Yoob (GitLab):
``repoach review gate <merged MR>`` ended rc 5 with ``stale head:
ls-remote failed:`` — an empty reason — because the merged MR's source
branch was deleted and ``git ls-remote`` answers rc 0 with no output.
The boundary fake is the GitLab forge's subprocess spawner, answering
the ``glab api`` and ``git ls-remote`` calls the real forge issues.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from repoach.cli.review_cmds import review_app
from repoach.forge.gitlab import GitLabForge
from repoach.forge.runner import CommandResult
from repoach.review import auto_merge
from repoach.review.auto_merge import OUTCOME_ALREADY_MERGED, evaluate_merge_gate

_SHA = "d" * 40


def _gitlab(tmp_path: Path, *, state: str, ls_remote: CommandResult) -> GitLabForge:
    mr = {
        "iid": 283,
        "state": state,
        "sha": _SHA,
        "source_branch": "feat/x",
        "target_branch": "develop",
        "head_pipeline": {"id": 1, "status": "success", "sha": _SHA},
    }
    glab = tmp_path / "glab"
    glab.write_text("")
    forge = GitLabForge(cwd=tmp_path, glab_path=str(glab), bot_username="bot")
    calls: list[list[str]] = []

    def spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        calls.append(argv)
        if Path(argv[0]).name == "git":
            assert argv[1:3] == ["ls-remote", "origin"], argv
            return CommandResult(ls_remote.returncode, ls_remote.stdout, ls_remote.stderr, argv)
        assert argv[1:] == ["api", "projects/:id/merge_requests/283"], argv
        return CommandResult(0, json.dumps(mr), "", argv)

    forge._spawn = spawn
    forge.spawned = calls
    return forge


def _evaluate(forge: GitLabForge, tmp_path: Path) -> auto_merge.GateEvaluation:
    return evaluate_merge_gate(
        283,
        forge=forge,
        db_path=tmp_path / "gate.db",
        wait_seconds=0,
        poll_interval=1,
        sleep=lambda _s: None,
        repo_root=tmp_path,
    )


_DELETED = CommandResult(0, "", "", [])


def test_merged_mr_is_already_merged(tmp_path: Path) -> None:
    forge = _gitlab(tmp_path, state="merged", ls_remote=_DELETED)
    evaluation = _evaluate(forge, tmp_path)
    assert evaluation.ci_gate.outcome_tag == OUTCOME_ALREADY_MERGED
    assert evaluation.decision.merge is False
    assert evaluation.decision.reasons == ["ALREADY_MERGED: #283 is already merged into 'develop'"]
    assert not any(Path(argv[0]).name == "git" for argv in forge.spawned)


def test_open_mr_with_deleted_branch_names_the_reason(tmp_path: Path) -> None:
    forge = _gitlab(tmp_path, state="opened", ls_remote=_DELETED)
    evaluation = _evaluate(forge, tmp_path)
    assert evaluation.decision.merge is False
    assert evaluation.decision.reasons == [
        "stale head: branch 'feat/x' not found on origin (deleted, or never pushed)"
    ]


def test_silent_ls_remote_failure_names_its_exit_code(tmp_path: Path) -> None:
    forge = _gitlab(tmp_path, state="opened", ls_remote=CommandResult(128, "", "", []))
    evaluation = _evaluate(forge, tmp_path)
    assert evaluation.decision.reasons == ["stale head: ls-remote failed: exit 128, no output"]


@pytest.mark.parametrize(
    ("state", "expected_rc", "expected_outcome"),
    [("merged", 0, OUTCOME_ALREADY_MERGED), ("opened", 5, "SKIP_STALE_HEAD")],
)
def test_gate_cli_exit_code(
    tmp_path: Path, state: str, expected_rc: int, expected_outcome: str
) -> None:
    forge = _gitlab(tmp_path, state=state, ls_remote=_DELETED)
    real = auto_merge.evaluate_merge_gate

    def with_scripted_forge(pr_number: int, **_kwargs: Any) -> auto_merge.GateEvaluation:
        return real(
            pr_number,
            forge=forge,
            db_path=tmp_path / "gate.db",
            wait_seconds=0,
            poll_interval=1,
            sleep=lambda _s: None,
            repo_root=tmp_path,
        )

    with patch("repoach.cli.review_cmds.evaluate_merge_gate", with_scripted_forge):
        result = CliRunner().invoke(review_app, ["gate", "283"])

    assert result.exit_code == expected_rc, result.output
    payload = json.loads(result.stdout)
    assert payload["outcome"] == expected_outcome
    assert payload["merge"] is False
    assert all(not reason.endswith(": ") for reason in payload["reasons"])
