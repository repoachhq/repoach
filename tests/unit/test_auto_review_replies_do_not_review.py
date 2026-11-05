"""A thread reply re-arms the merge without reviewing; a transport failure is retried once.

SP-REVIEW-SURVIVES-TRANSPORT-FAILURES G2 and G3, replayed against the
parsed ``auto-review.yml`` and ``merge-on-ci.yml`` with the concurrency
simulator (:mod:`tests.github_concurrency_sim`). Measured 2026-11-04 on
#288: every reply was a submitted review that started a bot review and
cancelled the one in progress (57 ``pull_request_review`` runs of Auto
Review cancelled in the last 100); and the 22 ``pull_request_review``
runs whose every job was skipped concluded ``skipped``, which
merge-on-ci.yml does not admit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from repoach.review.merge_wait import COALESCE_WINDOW_S
from repoach.review.review_retry import REVIEW_RETRY_DELAY_S
from tests.github_concurrency_sim import (
    Context,
    JobRecord,
    SimEvent,
    Simulator,
    dispatch_event,
    job_condition,
    pull_request_event,
    review_comment_event,
    review_event,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
AUTO_REVIEW = WORKFLOWS / "auto-review.yml"
REPOSITORY = "repoachhq/repoach"
DURATIONS = {"coalesce": 60, "rearm": 60, "review": 300, "auto_fix": 120, "auto_merge": 60}
OUTPUTS = {
    "review": {
        "verdict": "APPROVE_OR_COMMENT",
        "pr_number": "288",
        "head_ref": "fix/base-ref-must-resolve",
    },
    "auto_fix": {"pushed": "false"},
}


def _workflow(path: Path = AUTO_REVIEW) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _simulate(events: list[SimEvent]) -> list[JobRecord]:
    return Simulator.from_file(AUTO_REVIEW, durations=DURATIONS, outputs=OUTPUTS).run(events)


def _ran(records: list[JobRecord], job: str) -> list[JobRecord]:
    return [r for r in records if r.job == job and r.result != "skipped"]


def test_a_reply_never_starts_a_bot_review() -> None:
    """Eight replies a minute, as on #288: no review runs, none is cancelled, one re-arm survives."""
    events = [review_event(i * 7.5, 288) for i in range(8)]
    records = _simulate(events)

    assert _ran(records, "review") == []
    assert _ran(records, "auto_merge") == []
    rearmed = [r for r in records if r.job == "rearm" and r.result == "success"]
    assert [r.run for r in rearmed] == [7]


def test_a_reply_does_not_cancel_the_review_of_a_push() -> None:
    events = [pull_request_event(0.0, 288)] + [review_event(30.0 + i * 10, 288) for i in range(5)]
    records = _simulate(events)

    reviews = _ran(records, "review")
    assert [(r.run, r.result, r.cancelled_running) for r in reviews] == [(0, "success", False)]


def test_a_push_still_cancels_the_stale_review_of_a_push() -> None:
    records = _simulate([pull_request_event(0.0, 288), pull_request_event(100.0, 288)])

    assert [(r.run, r.result) for r in _ran(records, "review")] == [
        (0, "cancelled"),
        (1, "success"),
    ]


def test_an_owner_command_in_a_reply_is_reviewed_and_plain_text_is_not() -> None:
    records = _simulate(
        [
            review_comment_event(0.0, 288, "/resolve the ruleset lives outside the repository"),
            review_comment_event(400.0, 288, "thanks, fixed in the next push"),
            review_comment_event(800.0, 288, "/resolve forged", author_type="Bot"),
        ]
    )

    assert [(r.run, r.result) for r in _ran(records, "review")] == [(0, "success")]


def test_a_reply_run_concludes_success_and_merge_on_ci_admits_it() -> None:
    """The re-arm path of #286 holds: the reply's run succeeds, merge-on-ci re-judges the merge."""
    records = _simulate([review_event(0.0, 288)])
    assert {r.result for r in records} == {"success", "skipped"}
    assert [r.job for r in records if r.result == "success"] == ["rearm"]

    merge = _workflow(WORKFLOWS / "merge-on-ci.yml")
    completed = Context(
        github={
            "event_name": "workflow_run",
            "actor": "jwfaye",
            "repository": REPOSITORY,
            "event": {
                "workflow_run": {
                    "conclusion": "success",
                    "event": "pull_request_review",
                    "head_repository": {"full_name": REPOSITORY},
                    "actor": {"login": "jwfaye"},
                }
            },
        }
    )
    assert job_condition(merge["jobs"]["merge"]["if"], completed)


def test_no_review_event_reaches_the_review_job() -> None:
    """Fails if auto-review.yml ever reviews on a submitted review again, whoever wrote it."""
    jobs = _workflow()["jobs"]
    for user_type in ("User", "Bot"):
        event = review_event(0.0, 288, user_type=user_type)
        ctx = Context(
            github={
                "event_name": event.event_name,
                "actor": event.actor,
                "event": event.payload,
                "repository": REPOSITORY,
            },
            needs={"coalesce": {"result": "skipped", "outputs": {}}},
        )
        assert not job_condition(jobs["review"]["if"], ctx), user_type


def _retry_ctx(
    *, review_result: str, cause: str, retry_of: str = "", actor: str = "jwfaye"
) -> Context:
    event_name = "workflow_dispatch" if retry_of else "pull_request"
    payload: dict[str, Any] = (
        {"inputs": {"pr_number": "288", "retry_of": retry_of}}
        if retry_of
        else {"pull_request": {"number": 288, "draft": False}}
    )
    return Context(
        github={
            "event_name": event_name,
            "actor": actor,
            "event": payload,
            "repository": REPOSITORY,
        },
        needs={
            "review": {
                "result": review_result,
                "outputs": {"incomplete_cause": cause, "pr_number": "288", "head_sha": "cc59550"},
            }
        },
    )


def test_the_retry_job_runs_once_per_failed_head_and_never_retries_a_retry() -> None:
    retry = _workflow()["jobs"]["retry"]

    assert job_condition(retry["if"], _retry_ctx(review_result="failure", cause="transport"))
    assert not job_condition(retry["if"], _retry_ctx(review_result="failure", cause="other"))
    assert not job_condition(retry["if"], _retry_ctx(review_result="success", cause=""))
    assert not job_condition(
        retry["if"], _retry_ctx(review_result="failure", cause="transport", actor="mallory")
    )
    second = _retry_ctx(
        review_result="failure",
        cause="transport",
        retry_of="41916625939",
        actor="github-actions[bot]",
    )
    assert job_condition(retry["if"], second)
    steps = {step["name"]: step for step in retry["steps"]}
    wait = steps["Wait before the retry (review_retry_delay_s)"]
    assert wait["run"] == f"sleep {REVIEW_RETRY_DELAY_S}"
    assert not job_condition(wait["if"], second)
    assert "--retry-of" in steps["Dispatch the retry, or record the retry run"]["run"]
    assert retry["runs-on"] == "ubuntu-latest"
    assert retry["permissions"]["actions"] == "write"


def test_the_retry_run_reviews_and_merges_but_never_pushes() -> None:
    records = _simulate([dispatch_event(0.0, 288, retry_of="41916625939")])
    by_job = {r.job: r.result for r in records}

    assert by_job["review"] == "success"
    assert by_job["auto_merge"] == "success"
    assert by_job["auto_fix"] == "skipped"
    assert by_job["retry"] == "success"


def test_every_wait_of_the_workflow_is_its_named_constant() -> None:
    """The workflow's sleeps cannot drift from ``coalesce_window_s`` and ``review_retry_delay_s``."""
    jobs = _workflow()["jobs"]
    for name in ("coalesce", "rearm"):
        assert jobs[name]["steps"][0]["run"] == f"sleep {COALESCE_WINDOW_S}", name
    steps = {step["name"]: step for step in jobs["retry"]["steps"]}
    assert steps["Wait before the retry (review_retry_delay_s)"]["run"] == (
        f"sleep {REVIEW_RETRY_DELAY_S}"
    )


def test_the_review_step_never_imports_from_the_pr_checkout() -> None:
    """The step runs with the PR head as cwd; neither interpreter may put that cwd on sys.path."""
    steps = {step.get("name"): step for step in _workflow()["jobs"]["review"]["steps"]}
    script = steps["Run review-bot team"]["run"]
    after_cd = script.split("cd pr_head", 1)[1]
    calls = [line.strip() for line in after_cd.splitlines() if line.strip().startswith("python")]
    assert [call.split()[1] for call in calls] == ["-P", "-I"]


def test_a_bot_dispatch_without_retry_of_reviews_nothing() -> None:
    records = _simulate([dispatch_event(0.0, 288, retry_of="")])

    assert _ran(records, "review") == []
