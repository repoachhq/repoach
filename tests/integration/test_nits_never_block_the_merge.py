"""SP-REVIEW-NITS-ONE-COMMENT end to end: nits never open a thread, so they never hold the merge.

A real bare origin carries ``refs/pull/7/head``; ``review pr 7`` posts
through the real :class:`~repoach.forge.github.GitHubForge` with a
stubbed ``gh`` that plays GitHub in memory: a batched review's inline
comments become open review threads (GraphQL ``reviewThreads``), issue
comments are created and edited in place. The stub reviewers say what
each test scripts. The merge decision is then taken at the head on the
same forge.

The replay of Yoob MR !366 reads ``tests/fixtures/review/mr366_bot_threads.json``:
every bot thread the MR carried, grouped by the head it was raised at,
with its lens, severity, file and line (paths renamed, bodies replaced
by the note id).
"""

from __future__ import annotations

import functools
import itertools
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import typer

from repoach.cli import review_cmds
from repoach.forge.github import GhResult
from repoach.review import orchestrator
from repoach.review.auto_merge import decide_at_head
from repoach.review.nit_digest import NIT_DIGEST_MARKER
from repoach.review.orchestrator import run_review
from repoach.review.reviewer import BotRole, ReviewComment, ReviewerOutcome, ReviewVerdict
from tests.fixtures.generated_artefacts.harness import Forge, Repo, Review, install
from tests.forge_fakes import thread_node, threads_page

PR = 7
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "review" / "mr366_bot_threads.json"
SEVERITY_OPENS_A_THREAD = {"minor", "major", "blocker"}


class GitHub(Forge):
    """The harness forge, playing reviews, review threads and issue comments in memory."""

    def __init__(self, repo: Repo, head: str) -> None:
        super().__init__(repo, PR, head)
        self.inline: list[dict[str, object]] = []
        self.issue: list[dict[str, object]] = []
        self._ids = itertools.count(4437504238)

    def move_head(self, head: str) -> None:
        """Serve ``head`` as the pull request's head from now on."""
        self._head = head

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        endpoint = next((a for a in args[1:] if "/" in a and not a.startswith("-")), "")
        if args[:2] == ["pr", "view"]:
            payload = {
                "number": PR,
                "headRefName": "feat/x",
                "baseRefName": "main",
                "state": "OPEN",
                "url": f"https://github.com/acme/app/pull/{PR}",
            }
            return GhResult(0, json.dumps(payload), "", argv=args)
        if args[:3] == ["api", "--method", "POST"] and endpoint.endswith(f"/pulls/{PR}/reviews"):
            review_id = next(self._ids)
            for draft in json.loads(input_data or "{}").get("comments", []):
                self.inline.append(
                    {
                        "id": next(self._ids),
                        "pull_request_review_id": review_id,
                        "user": {"login": "bot"},
                        "path": draft["path"],
                        "line": draft["line"],
                        "body": draft["body"],
                    }
                )
            return GhResult(0, json.dumps({"id": review_id}), "", argv=args)
        if "/reviews/" in endpoint and endpoint.endswith("/comments"):
            review_id = int(endpoint.split("/")[-2])
            listed = [c for c in self.inline if c["pull_request_review_id"] == review_id]
            return GhResult(0, json.dumps(listed), "", argv=args)
        if endpoint.endswith(f"/pulls/{PR}/comments"):
            return GhResult(0, json.dumps([self.inline]), "", argv=args)
        if endpoint.endswith(f"/issues/{PR}/comments"):
            if "POST" in args:
                comment = {
                    "id": next(self._ids),
                    "user": {"login": "bot"},
                    "body": json.loads(input_data or "{}")["body"],
                }
                self.issue.append(comment)
                return GhResult(0, json.dumps(comment), "", argv=args)
            return GhResult(0, json.dumps([self.issue]), "", argv=args)
        if "PATCH" in args and "/issues/comments/" in endpoint:
            comment_id = int(endpoint.rsplit("/", 1)[-1])
            [comment] = [c for c in self.issue if c["id"] == comment_id]
            comment["body"] = json.loads(input_data or "{}")["body"]
            return GhResult(0, json.dumps(comment), "", argv=args)
        if "DELETE" in args and "/issues/comments/" in endpoint:
            comment_id = int(endpoint.rsplit("/", 1)[-1])
            self.issue = [c for c in self.issue if c["id"] != comment_id]
            return GhResult(0, "", "", argv=args)
        if args[:2] == ["api", "graphql"]:
            nodes = [
                thread_node(int(str(c["id"])), resolved=False, path=str(c["path"]), line=None)
                for c in self.inline
            ]
            return GhResult(0, json.dumps([threads_page(nodes)]), "", argv=args)
        return super()._run(args, input_data=input_data)

    def digests(self) -> list[dict[str, object]]:
        """The issue comments the nit digest marker opens."""
        return [c for c in self.issue if str(c["body"]).startswith(NIT_DIGEST_MARKER)]


@dataclass
class Script:
    """What each stub reviewer answers at the current round."""

    comments: dict[BotRole, list[ReviewComment]] = field(default_factory=dict)


class _ScriptedReviewer:
    def __init__(self, role: BotRole, script: Script) -> None:
        self.role = role
        self._script = script

    def review_diff(self, diff: str, **_kwargs: object) -> ReviewerOutcome:
        comments = list(self._script.comments.get(self.role, []))
        return ReviewerOutcome(
            role=self.role,
            verdict=ReviewVerdict.COMMENT if comments else ReviewVerdict.APPROVE,
            summary="scripted",
            comments=comments,
            model_used="stub",
            elapsed_s=0.0,
            tokens_used=0,
            raw_response="{}",
        )


def _install_script(monkeypatch: pytest.MonkeyPatch) -> Script:
    script = Script()
    for name, role in {
        "Architect": BotRole.ARCHITECT,
        "Sentinel": BotRole.SENTINEL,
        "Tester": BotRole.TESTER,
        "Scribe": BotRole.SCRIBE,
    }.items():
        monkeypatch.setattr(
            orchestrator, name, lambda _r=role, **_kw: _ScriptedReviewer(_r, script)
        )
    monkeypatch.setattr(orchestrator, "fire_review_routine", lambda *_a, **_k: None)
    return script


def _review(
    repo: Repo,
    forge: GitHub,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    dry_run: bool = False,
) -> Review:
    """Run ``review pr 7`` from the checkout through ``forge``."""
    monkeypatch.chdir(repo.checkout)
    monkeypatch.setattr(review_cmds, "run_review", functools.partial(run_review, forge=forge))
    exit_code = 0
    capsys.readouterr()
    try:
        review_cmds.review_pr(
            PR,
            dry_run=dry_run,
            forge_name=None,
            spec=None,
            provider=None,
            keep_tree=False,
            force=False,
            dry_run_reason="dry run",
            generated_evidence=None,
        )
    except typer.Exit as exited:
        exit_code = exited.exit_code
    out, err = capsys.readouterr()
    assert out, err
    return Review(json.loads(out), err.splitlines(), exit_code)


def _files_for(comments: Sequence[ReviewComment], round_n: int) -> dict[str, str]:
    """A content for every cited file, long enough for its cited lines, new at each round."""
    longest: dict[str, int] = {}
    for comment in comments:
        longest[comment.file] = max(longest.get(comment.file, 0), comment.line)
    return {
        path: "".join(f"line {n} of round {round_n}\n" for n in range(1, lines + 3))
        for path, lines in longest.items()
    }


def test_gate_is_green_with_eight_nits_and_no_thread(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Eight nits: no inline comment, one digest, and the gate at the head has no thread line."""
    bench = install(monkeypatch, tmp_path)
    script = _install_script(monkeypatch)
    nits = [
        ReviewComment(file="src/app.py", line=n, severity="nit", body=f"style point number {n}")
        for n in range(1, 9)
    ]
    script.comments[BotRole.SCRIBE] = nits
    repo = Repo(tmp_path / "repo", {"README.md": "base\n"})
    head = repo.push_head(_files_for(nits, 1))
    dry_forge = GitHub(repo, head)

    dry = _review(repo, dry_forge, monkeypatch, capsys, dry_run=True)
    assert dry.payload["nits"]["count"] == 8
    assert dry.payload["nits"]["digest_url"] is None
    assert str(dry.payload["nits"]["body"]).startswith(NIT_DIGEST_MARKER)
    assert dry_forge.issue == [] and dry_forge.inline == []

    forge = GitHub(repo, head)
    review = _review(repo, forge, monkeypatch, capsys)

    assert review.payload["final_verdict"] == "APPROVE"
    assert forge.inline == []
    [digest] = forge.digests()
    assert review.payload["nits"]["count"] == 8
    assert review.payload["nits"]["digest_url"] == (
        f"https://github.com/acme/app/pull/{PR}#issuecomment-{digest['id']}"
    )
    assert f"nit digest: 8 nit(s), {review.payload['nits']['digest_url']}" in review.stderr
    scribe = next(r for r in review.payload["reviews"] if r["role"] == "scribe")
    assert scribe["verdict"] == "APPROVE"
    assert scribe["summary"].endswith("[auto-promoted: no blocking comment]")
    assert review.payload["review_threads"]["unresolved"] == 0

    _, facts, decision = decide_at_head(
        PR, forge=forge, db=bench.db_path, ci_green=True, repo_root=repo.checkout, head_sha=head
    )
    assert facts.unresolved_threads == 0
    assert not any("unresolved review thread" in reason for reason in decision.reasons)
    assert decision.merge is True


def test_replaying_mr_366_posts_one_digest_and_a_thread_per_minor_or_worse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """C1: every recorded round of !366 reviewed again: one digest, one thread per minor or worse."""
    install(monkeypatch, tmp_path)
    script = _install_script(monkeypatch)
    rounds = json.loads(FIXTURE.read_text(encoding="utf-8"))["rounds"]
    repo = Repo(tmp_path / "repo", {"README.md": "base\n"})
    forge: GitHub | None = None
    expected_threads = 0
    expected_nits = 0
    for recorded in rounds:
        by_role: dict[BotRole, list[ReviewComment]] = {}
        for c in recorded["comments"]:
            by_role.setdefault(BotRole(c["role"]), []).append(
                ReviewComment(
                    file=c["path"], line=c["line"], severity=c["severity"], body=c["body"]
                )
            )
            if c["severity"] in SEVERITY_OPENS_A_THREAD:
                expected_threads += 1
            else:
                expected_nits += 1
        script.comments = by_role
        every = [comment for comments in by_role.values() for comment in comments]
        head = repo.push_head(_files_for(every, recorded["round"]))
        if forge is None:
            forge = GitHub(repo, head)
        forge.move_head(head)

        review = _review(repo, forge, monkeypatch, capsys)

        assert review.payload["head_sha"] == head
        assert len(forge.digests()) <= 1
    assert forge is not None
    assert expected_threads > 0 and expected_nits > 0
    assert len(forge.inline) == expected_threads
    [digest] = forge.digests()
    assert review.payload["nits"]["count"] == expected_nits
    assert NIT_DIGEST_MARKER in str(digest["body"])


def test_the_guard_never_moves_a_major_into_the_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A major the guard disproves ("no test for `_parse`", `_parse` on disk) still opens a thread."""
    bench = install(monkeypatch, tmp_path)
    script = _install_script(monkeypatch)
    major = ReviewComment(
        file="src/app.py",
        line=1,
        severity="major",
        body="No test for `_parse`: the zero-length branch raises and nothing covers it.",
    )
    script.comments = {BotRole.TESTER: [major]}
    repo = Repo(tmp_path / "repo", {"README.md": "base\n"})
    head = repo.push_head({"src/app.py": "def _parse(x):\n    return 1 / len(x)\n\n"})
    forge = GitHub(repo, head)

    review = _review(repo, forge, monkeypatch, capsys)

    assert [c["path"] for c in forge.inline] == ["src/app.py"]
    assert str(forge.inline[0]["body"]).startswith("**[tester/minor]**")
    assert forge.digests() == []
    assert review.payload["nits"]["count"] == 0
    _, _, decision = decide_at_head(
        PR, forge=forge, db=bench.db_path, ci_green=True, repo_root=repo.checkout, head_sha=head
    )
    assert "1 unresolved review thread(s)" in decision.reasons
    assert decision.merge is False
