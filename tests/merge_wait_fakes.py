"""The fixture forge of SP-AUTO-MERGE-WAITS-FOR-CHECKS-NOT-ITSELF.

:class:`MergeWaitFixtureForge` is the real :class:`~repoach.forge.github.GitHubForge`
with its ``gh`` and ``git`` seams answered from in-memory state: open PRs,
the check runs at each head (in the shape of
``gh api repos/{owner}/{repo}/commits/<sha>/check-runs --jq …``), the PR
conversation comments and the merge. Every ``gh`` argv is recorded, and so
is every write (an argv :func:`~repoach.forge.protocol.is_write_argv`
calls a write: comment POST/PATCH, ``pr merge``, ``workflow run``).
The clock is injectable: :attr:`now` moves only through :meth:`advance`.

The default PR is #259 at ``ba9585eb…`` on ``develop``, the head measured
on 2026-10-26 (F3). :func:`measured_check_runs` loads the check runs
recorded from ``repoachhq/repoach`` for the replayed heads
(``tests/fixtures/merge_wait/check_runs_<sha12>.ndjson``) and cuts them
at a decision time: a run that had not started is dropped, one that had
started but not completed is ``in_progress``.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from repoach.forge.github import GitHubForge
from repoach.forge.protocol import is_write_argv
from repoach.forge.runner import CommandResult
from repoach.review.findings import record_review_integrity

HEAD_259 = "ba9585eb07227b45168e907bd37b727cbafa1d1b"
HEAD_256 = "c19344e43bb3b2ec5cf091eae287877a121ed2a7"
HEAD_247 = "efb460f46b3d634966d0f6f6169aab60a288b76e"
HEAD_249 = "b86dc613d6953e8adedcc07a1ce36d7295da395a"
BOT = "github-actions[bot]"
REPOSITORY = "repoachhq/repoach"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "merge_wait"

CI_CHECKS: tuple[str, ...] = (
    "Test suite (Python 3.11)",
    "Test suite (Python 3.13)",
    "wheel-build",
    "wheel-smoke (3.11)",
    "wheel-smoke (3.13)",
)
"""The five ``CI`` job names (F11, D4)."""

PRODUCERS: dict[str, str] = {
    **{name: "CI" for name in CI_CHECKS},
    "Architect / Sentinel / Tester / Scribe": "Auto Review (review bots)",
    "Coder auto-fix loop": "Auto Review (review bots)",
    "Auto-merge to develop": "Auto Review (review bots)",
    "ShellCheck (warning-level)": "ShellCheck",
}


def ts(value: str) -> datetime:
    """Parse ``2026-10-26T07:55:27Z``."""
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def iso(value: datetime) -> str:
    """Render a UTC datetime the way GitHub does."""
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(
    name: str,
    *,
    head: str,
    status: str = "completed",
    conclusion: str | None = "success",
    started: str = "",
    completed: str | None = None,
) -> dict[str, object]:
    """One check run in the jq shape the forge reads."""
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": started,
        "completedAt": completed,
        "headSha": head,
    }


def measured_check_runs(head: str, cut: datetime | None = None) -> list[dict[str, object]]:
    """The check runs recorded at ``head``, as they stood at ``cut``.

    Recorded 2026-10-26 with ``gh api --paginate
    repos/repoachhq/repoach/commits/<sha>/check-runs``. A run started after
    ``cut`` is dropped; a run started but not completed by ``cut`` is
    ``in_progress`` with no conclusion; ``cut`` None keeps every run final.
    """
    path = FIXTURES / f"check_runs_{head[:12]}.ndjson"
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        raw = json.loads(line)
        started = ts(raw["started_at"])
        completed = ts(raw["completed_at"]) if raw.get("completed_at") else None
        if cut is not None and started > cut:
            continue
        if cut is not None and (completed is None or completed > cut):
            rows.append(
                run(
                    raw["name"],
                    head=head,
                    status="in_progress",
                    conclusion=None,
                    started=raw["started_at"],
                )
            )
            continue
        rows.append(
            run(
                raw["name"],
                head=head,
                conclusion=raw["conclusion"],
                started=raw["started_at"],
                completed=raw["completed_at"],
            )
        )
    return rows


@dataclass
class FixturePR:
    """One PR the fixture forge serves."""

    number: int
    head_sha: str
    head_ref: str
    base: str = "develop"
    state: str = "OPEN"
    draft: bool = False
    check_runs: list[dict[str, object]] = field(default_factory=list)
    comments: list[dict[str, object]] = field(default_factory=list)


class MergeWaitFixtureForge:
    """Serve PRs, check runs, comments and merges to a real ``GitHubForge``.

    Attributes:
        forge: The :class:`GitHubForge` under test.
        prs: The served PRs by number.
        now: The injectable clock.
        argv: Every ``gh`` argv, in order.
        writes: Every write argv, in order.
        merges: The PR numbers ``gh pr merge`` merged, in order.
        comment_writes: ``(pr, body)`` of every comment POST / PATCH.
        dispatches: ``workflow run`` heads, e.g. ``workflow_run merge-on-ci.yml pr=256``.
    """

    def __init__(
        self,
        prs: Iterable[FixturePR] | None = None,
        *,
        now: datetime | None = None,
        read_only: bool = False,
        merge_state: str = "UNSTABLE",
    ) -> None:
        listed = (
            list(prs)
            if prs is not None
            else [FixturePR(259, HEAD_259, "docs/sp-approved-spec-without-plan")]
        )
        self.prs = {pr.number: pr for pr in listed}
        self.now = now or ts("2026-10-26T07:58:57Z")
        self.merge_state = merge_state
        self.argv: list[list[str]] = []
        self.writes: list[list[str]] = []
        self.merges: list[int] = []
        self.comment_writes: list[tuple[int, str]] = []
        self.dispatches: list[str] = []
        self._next_comment = 1000
        self.forge = GitHubForge(gh_path="/nonexistent/gh", bot_login=BOT, read_only=read_only)
        self.forge.merge_status_retry_s = 0.0
        self.forge._run = self._run
        self.forge._run_git = self._git

    @property
    def pr(self) -> FixturePR:
        """The first served PR (#259 by default)."""
        return next(iter(self.prs.values()))

    def clock(self) -> datetime:
        """The current fixture time."""
        return self.now

    def advance(self, seconds: float) -> datetime:
        """Move the clock forward."""
        self.now = self.now + timedelta(seconds=seconds)
        return self.now

    def complete(self, name: str, conclusion: str = "success", *, pr: int | None = None) -> dict:
        """Complete the running ``name`` at the PR head now; return the ``workflow_run`` event.

        The event is the payload GitHub sends merge-on-ci when the
        workflow producing ``name`` completes (``workflow_run.completed``).
        """
        target = self.prs[pr] if pr is not None else self.pr
        for entry in target.check_runs:
            if entry["name"] == name and entry["status"] != "completed":
                entry.update(status="completed", conclusion=conclusion, completedAt=iso(self.now))
        return {
            "event_name": "workflow_run",
            "actor": "jwfaye",
            "repository": REPOSITORY,
            "event": {
                "action": "completed",
                "workflow_run": {
                    "name": PRODUCERS.get(name, "CI"),
                    "conclusion": "success",
                    "event": "pull_request",
                    "head_sha": target.head_sha,
                    "head_branch": target.head_ref,
                    "head_repository": {"full_name": REPOSITORY},
                    "actor": {"login": "jwfaye"},
                },
            },
        }

    def attempts_comment(self, pr: int | None = None) -> dict[str, object] | None:
        """The PR's merge-attempts comment payload, if any."""
        target = self.prs[pr] if pr is not None else self.pr
        for comment in target.comments:
            if "<!-- repoach-merge-attempts:v1 -->" in str(comment["body"]):
                return comment
        return None

    def _ok(self, args: list[str], stdout: str) -> CommandResult:
        return CommandResult(returncode=0, stdout=stdout, stderr="", argv=["gh", *args])

    def _fail(self, args: list[str], stderr: str) -> CommandResult:
        return CommandResult(returncode=1, stdout="", stderr=stderr, argv=["gh", *args])

    def _rollup(self, pr: FixturePR) -> list[dict[str, object]]:
        return [
            {
                "name": entry["name"],
                "status": str(entry["status"]).upper(),
                "conclusion": str(entry["conclusion"] or "").upper(),
                "startedAt": entry["startedAt"],
            }
            for entry in pr.check_runs
        ]

    def _view(self, pr: FixturePR, fields: str) -> dict[str, object]:
        full: dict[str, object] = {
            "number": pr.number,
            "title": f"PR {pr.number}",
            "body": "",
            "headRefName": pr.head_ref,
            "baseRefName": pr.base,
            "author": {"login": "jwfaye"},
            "state": pr.state,
            "url": f"https://github.com/{REPOSITORY}/pull/{pr.number}",
            "headRefOid": pr.head_sha,
            "isDraft": pr.draft,
            "mergeable": "MERGEABLE",
            "mergeStateStatus": self.merge_state,
            "statusCheckRollup": self._rollup(pr),
            "commits": [],
        }
        return {key: full[key] for key in fields.split(",") if key in full}

    def _run(self, args: list[str], *, input_data: str | None = None) -> CommandResult:
        self.forge.barrier.require_admitted(args)
        self.argv.append(list(args))
        if is_write_argv(args):
            self.writes.append(list(args))
        if args[:2] == ["pr", "view"]:
            pr = self.prs[int(args[2])]
            return self._ok(args, json.dumps(self._view(pr, args[-1])))
        if args[:2] == ["pr", "list"]:
            base = args[args.index("--base") + 1] if "--base" in args else None
            rows = [
                self._view(pr, "number,title,headRefName,baseRefName,author,url,headRefOid,isDraft")
                for pr in self.prs.values()
                if pr.state == "OPEN" and (base is None or pr.base == base)
            ]
            return self._ok(args, json.dumps(rows))
        if args[:2] == ["pr", "merge"]:
            pr = self.prs[int(args[2])]
            pr.state = "MERGED"
            self.merges.append(pr.number)
            sha = "f" * 40
            return self._ok(args, f"Merged pull request #{pr.number} ({sha})\n")
        if args[:2] == ["workflow", "run"]:
            fields = " ".join(a for a in args[3:] if a != "-f")
            self.dispatches.append(f"workflow_run {args[2]} {fields}")
            return self._ok(args, "")
        if args[0] == "api":
            return self._api(args, input_data)
        raise AssertionError(f"unexpected gh call: {args}")

    def _api(self, args: list[str], input_data: str | None = None) -> CommandResult:
        endpoint = next(a for a in args[1:] if "/" in a and not a.startswith("-"))
        if endpoint.endswith("/protection"):
            return self._fail(args, "gh: Branch not protected (HTTP 404)")
        if "/check-runs" in endpoint:
            sha = endpoint.split("/commits/")[1].split("/")[0]
            entries = [e for pr in self.prs.values() for e in pr.check_runs if e["headSha"] == sha]
            return self._ok(args, "\n".join(json.dumps(e) for e in entries))
        if "--method" in args:
            method = args[args.index("--method") + 1]
            if "--input" in args:
                body = json.loads(input_data or "{}")["body"]
            else:
                body = next(a[len("body=") :] for a in args if a.startswith("body="))
            if method == "POST":
                number = int(endpoint.split("/issues/")[1].split("/")[0])
                self._next_comment += 1
                comment = {"id": self._next_comment, "body": body, "user": {"login": BOT}}
                self.prs[number].comments.append(comment)
                self.comment_writes.append((number, body))
                return self._ok(args, json.dumps({"id": comment["id"]}))
            comment_id = int(endpoint.rsplit("/", 1)[1])
            for pr in self.prs.values():
                for comment in pr.comments:
                    if comment["id"] == comment_id:
                        comment["body"] = body
                        self.comment_writes.append((pr.number, body))
                        return self._ok(args, json.dumps({"id": comment_id}))
            return self._fail(args, "HTTP 404")
        if "/issues/" in endpoint and endpoint.endswith("/comments"):
            number = int(endpoint.split("/issues/")[1].split("/")[0])
            return self._ok(args, json.dumps([self.prs[number].comments]))
        raise AssertionError(f"unexpected gh api call: {args}")

    def _git(self, args: list[str]) -> CommandResult:
        if args[:2] == ["ls-remote", "origin"]:
            ref = args[2].removeprefix("refs/heads/")
            for pr in self.prs.values():
                if pr.head_ref == ref:
                    return CommandResult(
                        returncode=0,
                        stdout=f"{pr.head_sha}\t{args[2]}\n",
                        stderr="",
                        argv=["git", *args],
                    )
            return CommandResult(returncode=0, stdout="", stderr="", argv=["git", *args])
        return CommandResult(returncode=0, stdout="", stderr="", argv=["git", *args])


def ci_runs(
    head: str = HEAD_259,
    *,
    in_progress: Iterable[str] = (),
    finished: str = "2026-10-26T07:53:19Z",
) -> list[dict[str, object]]:
    """The five CI checks at ``head``: ``SUCCESS``, except those still ``in_progress``."""
    running = set(in_progress)
    rows: list[dict[str, object]] = []
    for name in CI_CHECKS:
        if name in running:
            rows.append(
                run(
                    name,
                    head=head,
                    status="in_progress",
                    conclusion=None,
                    started="2026-10-26T07:41:14Z",
                )
            )
        else:
            rows.append(run(name, head=head, started="2026-10-26T07:39:38Z", completed=finished))
    return rows


def seed_review(db: Path, pr_number: int, head: str) -> None:
    """Record a complete four-reviewer review at ``head``: the pure gate then permits."""
    record_review_integrity(db, pr_number=pr_number, head_sha=head, n_reviewers=4, n_unparsed=0)
