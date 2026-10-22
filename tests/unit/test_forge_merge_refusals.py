"""Merge refusals are read from forge facts, and only ``open`` changes accept writes.

GitLab's accept-MR endpoint refuses with a bare ``405 Method Not Allowed``
that names no reason, so :class:`~repoach.forge.gitlab.GitLabForge`
reads the MR first (``state``, ``sha``, ``detailed_merge_status``,
``head_pipeline.status``) and decides from those fields; the auto-merge
maps each named refusal to a non-fatal outcome and only an unexplained
refusal to ``FAILED``. Both forges pin the merge to the head the gate
decided at (``glab mr merge --sha`` / ``gh pr merge --match-head-commit``).
Unknown and ``locked`` states map to explicit states that refuse writes
(SP-UNIVERSAL-FORGE G7).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.forge import ForgeMergeRefusedError, ForgeStateRefusedError, MergeMethod, ReviewDraft
from repoach.forge.github import (
    MERGE_STATE_STATUS,
    MERGEABILITY_FIELDS,
    SQUASH_BODY_UNAVAILABLE,
    pull_request_from_github,
)
from repoach.forge.gitlab import DETAILED_MERGE_STATUS, GitLabForge, pull_request_from_gitlab
from repoach.forge.protocol import WRITABLE_STATES
from repoach.forge.runner import CommandResult
from repoach.review.auto_merge import (
    _REFUSAL_OUTCOMES,
    OUTCOME_SKIP_BLOCKED,
    OUTCOME_SKIP_CHANGES_REQUESTED,
    OUTCOME_SKIP_CI_PENDING,
    OUTCOME_SKIP_CONFLICT,
    OUTCOME_SKIP_DISCUSSIONS,
    OUTCOME_SKIP_DRAFT,
    OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED,
    OUTCOME_SKIP_NOT_APPROVED,
    OUTCOME_SKIP_STALE_HEAD,
    OUTCOME_SKIP_STATE,
    merge_change,
)
from tests.forge_fakes import gh_ok, scripted_github

SHA = "a" * 40
REAL_405 = (
    "PUT https://gitlab.com/api/v4/projects/o%2Fr/merge_requests/7/merge: "
    "405 {message: 405 Method Not Allowed}"
)


def _mr(**over: object) -> dict[str, object]:
    mr: dict[str, object] = {
        "iid": 7,
        "state": "opened",
        "sha": SHA,
        "source_branch": "feat/x",
        "target_branch": "develop",
        "detailed_merge_status": "mergeable",
        "head_pipeline": {"id": 1, "status": "success", "sha": SHA},
    }
    mr.update(over)
    return mr


def _gitlab(tmp_path: Path, mr: dict[str, object], merge_result: CommandResult | None):
    """A GitLabForge answering the MR read and (optionally) the merge; records argv."""
    glab = tmp_path / "glab"
    glab.write_text("")
    forge = GitLabForge(cwd=tmp_path, glab_path=str(glab), bot_username="bot")
    calls: list[list[str]] = []

    def spawn(argv: list[str], *, input_data: str | None = None) -> CommandResult:
        calls.append(argv[1:])
        if argv[1:3] == ["mr", "merge"]:
            assert merge_result is not None, "merge attempted although a fact predicted refusal"
            return merge_result
        return CommandResult(0, json.dumps(mr), "", argv)

    forge._spawn = spawn
    return forge, calls


@pytest.mark.parametrize(
    ("over", "reason", "outcome"),
    [
        ({"detailed_merge_status": "ci_must_pass"}, "pending", OUTCOME_SKIP_CI_PENDING),
        ({"detailed_merge_status": "ci_still_running"}, "pending", OUTCOME_SKIP_CI_PENDING),
        (
            {"detailed_merge_status": "mergeable", "head_pipeline": {"status": "running"}},
            "pending",
            OUTCOME_SKIP_CI_PENDING,
        ),
        ({"detailed_merge_status": "not_approved"}, "not_approved", OUTCOME_SKIP_NOT_APPROVED),
        (
            {"detailed_merge_status": "discussions_not_resolved"},
            "discussions_not_resolved",
            OUTCOME_SKIP_DISCUSSIONS,
        ),
        ({"detailed_merge_status": "conflict"}, "conflict", OUTCOME_SKIP_CONFLICT),
        ({"sha": "b" * 40}, "head_moved", OUTCOME_SKIP_STALE_HEAD),
    ],
)
def test_gitlab_refusal_named_from_mr_fields_without_merging(
    tmp_path: Path, over: dict[str, object], reason: str, outcome: str
) -> None:
    forge, calls = _gitlab(tmp_path, _mr(**over), merge_result=None)
    with pytest.raises(ForgeMergeRefusedError) as info:
        forge.merge(7, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA)
    assert info.value.reason == reason
    assert all(c[:2] != ["mr", "merge"] for c in calls)
    assert _REFUSAL_OUTCOMES[reason] == outcome


def test_gitlab_bare_405_is_failed_not_pending(tmp_path: Path) -> None:
    """A 405 with no explaining fact is ``refused`` → FAILED, never SKIP_CI_PENDING."""
    forge, calls = _gitlab(tmp_path, _mr(), CommandResult(1, "", REAL_405, ["glab"]))
    ok, sha, log, refusal = merge_change(
        forge, 7, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA
    )
    assert (ok, sha, refusal) == (False, None, "refused")
    assert refusal not in _REFUSAL_OUTCOMES
    assert "405" in log
    merge_argv = next(c for c in calls if c[:2] == ["mr", "merge"])
    assert merge_argv[-2:] == ["--sha", SHA]


def test_gitlab_409_is_head_moved(tmp_path: Path) -> None:
    """GitLab's documented 409 (``sha`` no longer the head) is a stale head."""
    refusal_409 = (
        "PUT https://gitlab.com/api/v4/projects/o%2Fr/merge_requests/7/merge: "
        "409 {message: SHA does not match HEAD of source branch}"
    )
    forge, _ = _gitlab(tmp_path, _mr(), CommandResult(1, "", refusal_409, ["glab"]))
    with pytest.raises(ForgeMergeRefusedError) as info:
        forge.merge(7, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA)
    assert info.value.reason == "head_moved"


def _gh_view(**over: object) -> dict[str, object]:
    view: dict[str, object] = {
        "state": "OPEN",
        "isDraft": False,
        "mergeable": "MERGEABLE",
        "mergeStateStatus": "CLEAN",
        "headRefOid": SHA,
        "statusCheckRollup": [],
        "commits": [],
    }
    view.update(over)
    return view


def _github(views: list[dict[str, object]]):
    """A scripted GitHubForge serving the barrier, then ``views`` in order, then the merge."""
    queue = list(views)

    def run(args: list[str]) -> CommandResult:
        if args[:2] == ["pr", "view"] and args[-1] == "state":
            return gh_ok(json.dumps({"state": "OPEN"}), argv=args)
        if args[:2] == ["pr", "view"] and args[-1] == MERGEABILITY_FIELDS:
            return gh_ok(json.dumps(queue.pop(0)), argv=args)
        assert args[:2] == ["pr", "merge"], args
        return gh_ok(f"Merged ({SHA})\n", argv=args)

    forge = scripted_github(run=run)
    forge.merge_status_retry_s = 0.0
    return forge


def _merge_calls(forge) -> list[list[str]]:
    return [c.args[0] for c in forge._run.call_args_list if c.args[0][:2] == ["pr", "merge"]]


def test_github_merge_pins_head_with_match_head_commit() -> None:
    forge = _github([_gh_view()])
    forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert [c.args[0][:2] + c.args[0][-1:] for c in forge._run.call_args_list[:2]] == [
        ["pr", "view", "state"],
        ["pr", "view", MERGEABILITY_FIELDS],
    ]
    assert forge._run.call_args.args[0] == [
        "pr",
        "merge",
        "42",
        "--squash",
        "--match-head-commit",
        SHA,
        "--delete-branch",
        "--body-file",
        "-",
    ]


def test_squash_body_strips_ai_attribution() -> None:
    """A squash sends every commit message on stdin minus attribution (SP-CONVENTIONS-GATE G9)."""
    trailer = "Co-Authored-By: Claude Sonnet 5 <noreply@anthropic.com>"
    commits = [
        {"messageHeadline": "feat(x): add the parser", "messageBody": f"Why one.\n\n{trailer}"},
        {
            "messageHeadline": "fix(x): handle empty input",
            "messageBody": f"\U0001f916 Generated with [Claude Code](https://claude.com/claude-code)\n\n{trailer}",
        },
    ]
    forge = _github([_gh_view(commits=commits)])
    forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    call = forge._run.call_args
    assert call.args[0][-2:] == ["--body-file", "-"]
    body = call.kwargs["input_data"]
    assert body == "* feat(x): add the parser\n\nWhy one.\n\n* fix(x): handle empty input\n"
    assert "anthropic" not in body.lower()
    assert "claude" not in body.lower()


def test_squash_with_no_commits_merges_with_an_empty_body() -> None:
    """``commits: []`` is a well-formed view: the squash runs with an empty stdin body."""
    forge = _github([_gh_view(commits=[])])
    result = forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert result.merged is True
    call = forge._run.call_args
    assert call.args[0][:2] == ["pr", "merge"]
    assert call.args[0][-2:] == ["--body-file", "-"]
    assert call.kwargs["input_data"] == ""


def test_squash_refuses_a_view_without_commits() -> None:
    """A view missing ``commits`` is malformed: refused by name, ``gh pr merge`` never runs."""
    view = _gh_view()
    del view["commits"]
    forge = _github([view, dict(view)])
    with pytest.raises(ForgeMergeRefusedError) as excinfo:
        forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert excinfo.value.reason == "refused"
    assert excinfo.value.message == SQUASH_BODY_UNAVAILABLE
    assert _merge_calls(forge) == []
    ok, sha, log, refusal = merge_change(forge, 42, head_sha=SHA)
    assert (ok, sha, refusal) == (False, None, "refused")
    assert log == SQUASH_BODY_UNAVAILABLE


def test_merge_method_sends_no_body() -> None:
    """A merge commit keeps GitHub's own message: no ``--body-file``, no stdin."""
    forge = _github([_gh_view(commits=[{"messageHeadline": "feat: x", "messageBody": ""}])])
    forge.merge(42, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA)
    call = forge._run.call_args
    assert "--body-file" not in call.args[0]
    assert call.kwargs["input_data"] is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("opened", "open"), ("locked", "locked"), ("merged", "merged"), ("weird", "unknown")],
)
def test_gitlab_states_fail_closed(raw: str, expected: str) -> None:
    state = pull_request_from_gitlab({"iid": 1, "state": raw}).state
    assert state == expected
    assert (state in WRITABLE_STATES) is (expected == "open")


def test_github_unknown_state_fails_closed() -> None:
    state = pull_request_from_github({"number": 1, "state": "DRAFTISH"}, number=1).state
    assert state == "unknown" and state not in WRITABLE_STATES


def test_auto_merge_refuses_a_locked_mr_before_any_write(tmp_path: Path) -> None:
    from repoach.review.auto_merge import run_auto_merge

    forge, calls = _gitlab(tmp_path, _mr(state="locked"), merge_result=None)
    result = run_auto_merge(
        7,
        forge=forge,
        db_path=tmp_path / "merge.db",
        wait_seconds=0,
        poll_interval=1,
        sleep=lambda _s: None,
        repo_root=tmp_path,
        merge_method=MergeMethod.MERGE,
        delete_branch=False,
    )
    assert result.outcome == OUTCOME_SKIP_STATE
    assert all(c[:2] != ["mr", "merge"] for c in calls)


RUNNING = [{"name": "Test suite (Python 3.11)", "status": "IN_PROGRESS", "conclusion": None}]


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"mergeStateStatus": "BLOCKED", "statusCheckRollup": RUNNING}, "pending"),
        ({"mergeStateStatus": "BEHIND", "statusCheckRollup": RUNNING}, "pending"),
        ({"mergeStateStatus": "UNSTABLE", "statusCheckRollup": RUNNING}, "pending"),
        ({"mergeStateStatus": "BLOCKED"}, "blocked"),
        ({"mergeStateStatus": "BEHIND"}, "behind"),
        ({"mergeStateStatus": "DIRTY"}, "conflict"),
        ({"mergeable": "CONFLICTING"}, "conflict"),
        ({"mergeStateStatus": "DRAFT"}, "draft"),
        ({"isDraft": True}, "draft"),
        ({"headRefOid": "b" * 40}, "head_moved"),
        ({"mergeStateStatus": "SOMETHING_NEW"}, "unrecognized"),
    ],
)
def test_github_refusal_named_from_view_without_merging(
    over: dict[str, object], reason: str
) -> None:
    forge = _github([_gh_view(**over)])
    with pytest.raises(ForgeMergeRefusedError) as info:
        forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert info.value.reason == reason
    assert _merge_calls(forge) == []
    assert reason in _REFUSAL_OUTCOMES


def test_github_unstable_without_pending_checks_merges() -> None:
    forge = _github([_gh_view(mergeStateStatus="UNSTABLE")])
    forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert len(_merge_calls(forge)) == 1


def test_github_unknown_is_reread_once_then_merges_when_clean() -> None:
    forge = _github([_gh_view(mergeStateStatus="UNKNOWN"), _gh_view()])
    forge.merge(42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA)
    assert len(_merge_calls(forge)) == 1


def test_github_unknown_twice_is_failed() -> None:
    forge = _github([_gh_view(mergeStateStatus="UNKNOWN"), _gh_view(mergeStateStatus="UNKNOWN")])
    ok, _sha, _log, refusal = merge_change(
        forge, 42, method=MergeMethod.SQUASH, delete_branch=True, head_sha=SHA
    )
    assert (ok, refusal) == (False, "refused")
    assert refusal not in _REFUSAL_OUTCOMES
    assert _merge_calls(forge) == []


def test_github_merge_state_table_covers_every_documented_value() -> None:
    """https://docs.github.com/en/graphql/reference/enums#mergestatestatus"""
    documented = {
        "BEHIND",
        "BLOCKED",
        "CLEAN",
        "DIRTY",
        "DRAFT",
        "HAS_HOOKS",
        "UNKNOWN",
        "UNSTABLE",
    }
    assert documented <= set(MERGE_STATE_STATUS)


GITLAB_DOCUMENTED_DETAILED_MERGE_STATUS = {
    "approvals_syncing",
    "checking",
    "ci_must_pass",
    "ci_still_running",
    "commits_status",
    "conflict",
    "discussions_not_resolved",
    "draft_status",
    "jira_association_missing",
    "mergeable",
    "merge_request_blocked",
    "merge_time",
    "need_rebase",
    "not_approved",
    "not_open",
    "preparing",
    "requested_changes",
    "security_policy_pipeline_check",
    "security_policy_violations",
    "status_checks_must_pass",
    "unchecked",
    "locked_paths",
    "locked_lfs_files",
    "title_regex",
}
"""The ``detailed_merge_status`` values of https://docs.gitlab.com/ee/api/merge_requests.html#merge-status.

Read 2026-10-19 from gitlab-org/gitlab ``master:doc/api/merge_requests.md``
("Merge status" section). ``blocked_status``, ``policies_denied`` and
``external_status_checks`` are older values the table also maps.
"""


def test_gitlab_table_covers_every_documented_value() -> None:
    assert set(DETAILED_MERGE_STATUS) >= GITLAB_DOCUMENTED_DETAILED_MERGE_STATUS
    assert {"blocked_status", "policies_denied"} <= set(DETAILED_MERGE_STATUS)
    assert [k for k, v in DETAILED_MERGE_STATUS.items() if v is None] == ["mergeable"]
    for reason in DETAILED_MERGE_STATUS.values():
        assert reason is None or reason in _REFUSAL_OUTCOMES


@pytest.mark.parametrize(
    ("status", "outcome"),
    [
        ("unchecked", OUTCOME_SKIP_CI_PENDING),
        ("preparing", OUTCOME_SKIP_CI_PENDING),
        ("approvals_syncing", OUTCOME_SKIP_CI_PENDING),
        ("checking", OUTCOME_SKIP_CI_PENDING),
        ("draft_status", OUTCOME_SKIP_DRAFT),
        ("requested_changes", OUTCOME_SKIP_CHANGES_REQUESTED),
        ("blocked_status", OUTCOME_SKIP_BLOCKED),
        ("policies_denied", OUTCOME_SKIP_BLOCKED),
        ("jira_association_missing", OUTCOME_SKIP_BLOCKED),
        ("a_value_gitlab_adds_later", OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED),
        ("", OUTCOME_SKIP_MERGE_STATUS_UNRECOGNIZED),
    ],
)
def test_gitlab_non_mergeable_status_never_reaches_the_merge_call(
    tmp_path: Path, status: str, outcome: str
) -> None:
    forge, calls = _gitlab(tmp_path, _mr(detailed_merge_status=status), merge_result=None)
    ok, _sha, _log, refusal = merge_change(
        forge, 7, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA
    )
    assert not ok and _REFUSAL_OUTCOMES[refusal] == outcome
    assert all(c[:2] != ["mr", "merge"] for c in calls)


WRITE_CALLS = {
    "post_comment": lambda f, n: f.post_comment(n, "hello"),
    "post_review": lambda f, n: f.post_review(n, ReviewDraft(verdict="COMMENT", body="b")),
    "reply": lambda f, n: f.reply(n, 1, "evidence"),
    "upsert_marked_comment": lambda f, n: f.upsert_marked_comment(n, "<!-- m -->", "b"),
    "merge": lambda f, n: f.merge(n, method=MergeMethod.MERGE, delete_branch=False),
}


@pytest.mark.parametrize("write", sorted(WRITE_CALLS))
@pytest.mark.parametrize("raw_state", ["merged", "closed", "locked", "reopened_somehow"])
def test_gitlab_write_barrier_refuses_every_write(
    tmp_path: Path, write: str, raw_state: str
) -> None:
    forge, calls = _gitlab(tmp_path, _mr(state=raw_state), merge_result=None)
    with pytest.raises(ForgeStateRefusedError) as info:
        WRITE_CALLS[write](forge, 7)
    assert info.value.state == pull_request_from_gitlab({"iid": 7, "state": raw_state}).state
    assert calls == [["api", "projects/:id/merge_requests/7"]]


@pytest.mark.parametrize("write", sorted(WRITE_CALLS))
@pytest.mark.parametrize("raw_state", ["MERGED", "CLOSED", "SOMETHING_NEW"])
def test_github_write_barrier_refuses_every_write(write: str, raw_state: str) -> None:
    forge = scripted_github(run=lambda a: gh_ok(json.dumps({"state": raw_state}), argv=a))
    with pytest.raises(ForgeStateRefusedError):
        WRITE_CALLS[write](forge, 42)
    assert [c.args[0] for c in forge._run.call_args_list] == [
        ["pr", "view", "42", "--json", "state"]
    ]


def test_gitlab_locked_merge_maps_to_skip_state(tmp_path: Path) -> None:
    forge, calls = _gitlab(tmp_path, _mr(state="locked"), merge_result=None)
    ok, _sha, _log, refusal = merge_change(
        forge, 7, method=MergeMethod.MERGE, delete_branch=False, head_sha=SHA
    )
    assert not ok and _REFUSAL_OUTCOMES[refusal] == OUTCOME_SKIP_STATE
    assert all(c[:2] != ["mr", "merge"] for c in calls)
