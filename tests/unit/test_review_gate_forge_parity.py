"""SP-UNIVERSAL-FORGE AC6 — the merge gate's facts are unchanged by the Forge protocol.

``tests/fixtures/forge/gate/pr202_capture.json`` holds the read-only
answers ``gh`` and ``git`` gave on 2026-10-19 for repoachhq/repoach
PR #202 (``pr view``, the head commit's check-runs, the branch
protection, ``git ls-remote``). ``pr202_facts_before_forge.json`` is the
JSON ``repoach review gate 202`` printed when ``evaluate_merge_gate`` ran
on those answers with the pre-protocol ``GhCli`` (``develop`` at
``20dbb45``). The same answers, served through the real
:class:`~repoach.forge.github.GitHubForge`, must produce the same object.
"""

from __future__ import annotations

import json
from pathlib import Path

from repoach.forge.gitlab import GitLabForge
from repoach.forge.runner import CommandResult
from repoach.review.auto_merge import evaluate_merge_gate
from tests.forge_fakes import scripted_github

GATE = Path(__file__).resolve().parent.parent / "fixtures" / "forge" / "gate"


def _answer(entry: dict[str, object], argv: list[str]) -> CommandResult:
    return CommandResult(
        returncode=int(str(entry["returncode"])),
        stdout=str(entry["stdout"]),
        stderr=str(entry["stderr"]),
        argv=argv,
    )


def test_gate_facts_identical_to_pre_protocol_recording(tmp_path: Path) -> None:
    capture = json.loads((GATE / "pr202_capture.json").read_text())
    expected = json.loads((GATE / "pr202_facts_before_forge.json").read_text())
    head = json.loads(capture["pr_view"]["stdout"])["headRefOid"]

    def run(args: list[str]) -> CommandResult:
        if args[:2] == ["pr", "view"] and "statusCheckRollup" in args:
            return _answer(capture["rollup"], args)
        if args[:2] == ["pr", "view"] and args[-1] == "headRefOid":
            return CommandResult(0, json.dumps({"headRefOid": head}), "", args)
        if args[:2] == ["pr", "view"]:
            return _answer(capture["pr_view"], args)
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            return _answer(capture["check_runs"], args)
        if args[:1] == ["api"] and args[1].endswith("/protection"):
            return _answer(capture["protection"], args)
        raise AssertionError(f"unexpected gh call {args}")

    def git(args: list[str]) -> CommandResult:
        assert args[:2] == ["ls-remote", "origin"], args
        return _answer(capture["ls_remote"], args)

    evaluation = evaluate_merge_gate(
        capture["pr"],
        forge=scripted_github(run=run, git=git),
        db_path=tmp_path / "gate.db",
        wait_seconds=0,
        poll_interval=1,
        sleep=lambda _s: None,
        repo_root=tmp_path,
    )
    facts = evaluation.facts
    actual = {
        "pr_number": capture["pr"],
        "head_sha": evaluation.head_sha,
        "merge": evaluation.decision.merge,
        "reasons": evaluation.decision.reasons,
        "facts": {
            "ci_green": facts.ci_green,
            "open_blocking_findings": facts.open_blocking_findings,
            "spec_covered": facts.spec_covered,
            "spec_coverage_known": facts.spec_coverage_known,
            "review_complete": facts.review_complete,
            "review_integrity_known": facts.review_integrity_known,
        },
    }
    assert actual == expected


def _gitlab_gate(tmp_path: Path, pipeline_status: str) -> bool:
    """Run the gate on a GitLab MR whose head pipeline reports ``pipeline_status``."""
    tmp_path.mkdir()
    sha = "c" * 40
    mr = {
        "iid": 9,
        "state": "opened",
        "sha": sha,
        "source_branch": "feat/x",
        "target_branch": "develop",
        "head_pipeline": {"id": 1, "status": pipeline_status, "sha": sha},
    }
    glab = tmp_path / "glab"
    glab.write_text("")
    forge = GitLabForge(cwd=tmp_path, glab_path=str(glab), bot_username="bot")

    def spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        if Path(argv[0]).name == "git":
            assert argv[1:3] == ["ls-remote", "origin"], argv
            return CommandResult(0, f"{sha}\trefs/heads/feat/x\n", "", argv)
        assert argv[1:] == ["api", "projects/:id/merge_requests/9"], argv
        return CommandResult(0, json.dumps(mr), "", argv)

    forge._spawn = spawn
    evaluation = evaluate_merge_gate(
        9,
        forge=forge,
        db_path=tmp_path / "gate.db",
        wait_seconds=0,
        poll_interval=1,
        sleep=lambda _s: None,
        repo_root=tmp_path,
    )
    return evaluation.facts.ci_green


def test_gitlab_gate_ci_green_is_head_pipeline_success(tmp_path: Path) -> None:
    """AC5: ``facts.ci_green`` equals ``head_pipeline.status == "success"``."""
    assert _gitlab_gate(tmp_path / "a", "success") is True
    assert _gitlab_gate(tmp_path / "b", "failed") is False
    assert _gitlab_gate(tmp_path / "c", "running") is False
