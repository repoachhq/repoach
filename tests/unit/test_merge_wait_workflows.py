"""The workflows coalesce owner bursts and re-arm every required check (AC5, AC6, AC7).

SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF G2 and G4. The concurrency
simulator (:mod:`tests.github_concurrency_sim`) replays events against
the parsed ``auto-review.yml``: today's copy
(``tests/fixtures/merge_wait/auto-review.before.yml``) reproduces F9,
the new one lets the last event of a burst through.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from repoach.review.merge_wait import (
    MergeWaitConfigError,
    assert_rearmed,
    rearm_workflows,
    required_checks,
    workflow_checks,
)
from tests.github_concurrency_sim import (
    JobRecord,
    SimEvent,
    Simulator,
    comment_event,
    review_event,
)
from tests.merge_wait_fakes import CI_CHECKS, ts

REPO_ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = REPO_ROOT / ".github" / "workflows"
BEFORE = REPO_ROOT / "tests" / "fixtures" / "merge_wait" / "auto-review.before.yml"
DURATIONS = {"coalesce": 60, "review": 300, "auto_fix": 120, "auto_merge": 60}
OUTPUTS = {
    "review": {
        "verdict": "APPROVE_OR_COMMENT",
        "pr_number": "246",
        "head_ref": "docs/sp-agent-context-loop",
    },
    "auto_fix": {"pushed": "false"},
}


def _load(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _simulate(path: Path, events: list[SimEvent]) -> list[JobRecord]:
    return Simulator.from_file(path, durations=DURATIONS, outputs=OUTPUTS).run(events)


def _completed(records: list[JobRecord], job: str) -> list[JobRecord]:
    return [r for r in records if r.job == job and r.result == "success"]


def _burst(start: float, reviews: int, spacing: float) -> list[SimEvent]:
    events = [review_event(start + i * spacing, 246) for i in range(reviews)]
    events.append(comment_event(start + reviews * spacing + 5, 246))
    return events


def test_fourteen_review_events_coalesce() -> None:
    events = _burst(0.0, 14, 8.5)
    assert events[-1].at <= 125 and events[-2].at < 120

    after = _simulate(WORKFLOWS / "auto-review.yml", events)
    assert len(_completed(after, "review")) == 1
    comment_run = max(r.run for r in after)
    assert all(r.result == "skipped" for r in after if r.run == comment_run)
    assert not [r for r in after if r.job in {"auto_fix", "auto_merge"} and r.cancelled_running]

    before = _simulate(BEFORE, events)
    assert len(_completed(before, "review")) == 0
    assert [r.result for r in before if r.run == comment_run and r.job == "review"] == ["skipped"]


def test_246_bursts_each_review_once() -> None:
    origin = ts("2026-10-25T15:53:57Z")
    starts = [
        "2026-10-25T15:53:57Z",
        "2026-10-25T16:04:00Z",
        "2026-10-25T19:03:00Z",
        "2026-10-25T19:21:05Z",
    ]
    events: list[SimEvent] = []
    for start in starts:
        events.extend(_burst((ts(start) - origin).total_seconds(), 4, 3.0))

    after = _simulate(WORKFLOWS / "auto-review.yml", events)
    before = _simulate(BEFORE, events)

    assert len(_completed(after, "review")) == 4
    assert not [r for r in after if r.job in {"auto_fix", "auto_merge"} and r.cancelled_running]
    assert len(_completed(before, "review")) == 0
    assert not [r for r in before if r.job in {"auto_fix", "auto_merge"} and r.started is not None]


def test_workflow_shape() -> None:
    review = _load(WORKFLOWS / "auto-review.yml")
    assert "concurrency" not in review
    jobs = review["jobs"]

    coalesce = jobs["coalesce"]
    assert coalesce["runs-on"] == "ubuntu-latest"
    assert coalesce["concurrency"]["group"].startswith("coalesce-")
    assert coalesce["concurrency"]["cancel-in-progress"] is True
    assert "github.event.comment.user.type != 'Bot'" in coalesce["if"]
    assert "github.event.review.user.type != 'Bot'" in coalesce["if"]
    assert "sleep 60" in coalesce["steps"][0]["run"]

    assert jobs["review"]["needs"] == "coalesce"
    assert jobs["review"]["concurrency"]["group"].startswith("review-")
    for name in ("auto_fix", "auto_merge"):
        assert jobs[name]["concurrency"] == {
            "group": "merge-${{ needs.review.outputs.head_ref }}",
            "cancel-in-progress": False,
        }

    merge = _load(WORKFLOWS / "merge-on-ci.yml")
    triggers = merge[True]
    assert triggers["workflow_run"]["workflows"] == ["CI", "Auto Review (review bots)"]
    assert "pr" in triggers["workflow_dispatch"]["inputs"]
    condition = merge["jobs"]["merge"]["if"]
    assert "github.event.workflow_run.event == 'pull_request'" in condition
    assert "github.event.workflow_run.event == 'pull_request_review'" in condition
    assert merge["concurrency"]["group"].startswith(
        "merge-${{ github.event.workflow_run.head_branch"
    )
    assert merge["concurrency"]["cancel-in-progress"] is False
    steps = [
        step.get("uses", "") + str(step.get("with", "")) for step in merge["jobs"]["merge"]["steps"]
    ]
    assert not [s for s in steps if "upload-artifact" in s]


def test_every_required_check_is_rearmed(tmp_path: Path) -> None:
    names = required_checks(REPO_ROOT)
    assert names == list(CI_CHECKS)
    produced = workflow_checks(REPO_ROOT)
    rearming = rearm_workflows(REPO_ROOT)
    for name in names:
        assert any(name in produced.get(w, []) for w in rearming), name
    assert assert_rearmed(REPO_ROOT) == names

    shutil.copytree(WORKFLOWS, tmp_path / ".github" / "workflows")
    (tmp_path / "repoach.toml").write_text(
        '[auto_merge]\nrequired_checks = ["ShellCheck (warning-level)"]\n', encoding="utf-8"
    )
    with pytest.raises(MergeWaitConfigError) as info:
        assert_rearmed(tmp_path)
    assert "not re-armed by any of ['CI', 'Auto Review (review bots)']" in str(info.value)
