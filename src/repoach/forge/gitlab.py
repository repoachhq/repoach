"""GitLab implementation of the :class:`~repoach.forge.protocol.Forge` protocol.

Transport is ``glab`` — its verbs where one exists (``glab mr merge``,
``glab mr create``) and ``glab api`` (authenticated REST with the
``:id`` placeholder resolved from the clone's remote) everywhere else,
since ``glab mr diff`` drops the ``diff --git`` headers repoach parses
and ``glab`` has no verb for discussions, positions or approvals.

Mapping onto the protocol (measured on Yoob MR !283, W1 table A1–A15):

* number → MR ``iid``; ``source_branch``/``target_branch``/``sha``/
  ``description``/``web_url``/``author.username`` → the
  :class:`~repoach.forge.protocol.PullRequest` fields; ``opened`` and
  ``locked`` → ``open``.
* diff → ``merge_requests/:iid/raw_diffs`` (git format), rebuilt from
  ``merge_requests/:iid/diffs`` when that endpoint is absent.
* inline comments → the ``DiffNote`` notes of the MR discussions; the
  first note of a discussion is the thread root, later notes reply to it.
* conversation comments → non-system, non-diff notes (oldest first).
* a review → one discussion per inline comment, anchored by
  ``position`` (``base_sha``/``start_sha``/``head_sha`` from the MR's
  ``diff_refs``), plus one note carrying the verdict body. The MR is
  never approved (spec Q1: note only during the advisory period).
* CI → ``head_pipeline.status``; jobs of that pipeline as checks.
* trust → ``author.username`` against the configured bot username; an
  empty username trusts nothing.

``glab api`` exits 0 on an HTTP error and reports it on stderr as
``(HTTP <code>)``; every call is therefore judged on both the exit code
and that status.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from ..core.logging import get_logger
from .protocol import (
    BranchProtection,
    CheckRun,
    CheckStatus,
    Comment,
    Diff,
    ForgeError,
    ForgeForbiddenError,
    ForgeMergeRefusedError,
    ForgeNotFoundError,
    ForgeUnavailableError,
    MergeMethod,
    MergeRefusal,
    MergeResult,
    PullRequest,
    PullRequestState,
    Review,
    ReviewDraft,
    diff_files,
    trusted,
    write_barrier,
)
from .runner import CommandResult, spawn

_log = get_logger(__name__)

PROJECT = "projects/:id"
"""``glab api`` placeholder for the clone's project (resolved by ``glab``)."""

_HTTP_STATUS_RE = re.compile(r"\(HTTP (\d{3})\)")
_JOB_LINK_RE = re.compile(r"/-/jobs/(\d+)")
_MR_URL_RE = re.compile(r"/merge_requests/(\d+)")

_STATE_TO_PROTOCOL: dict[str, PullRequestState] = {
    "opened": "open",
    "locked": "locked",
    "merged": "merged",
    "closed": "closed",
}

_PIPELINE_SUCCESS = frozenset({"success"})
_PIPELINE_FAILURE = frozenset({"failed", "canceled", "skipped"})
_PIPELINE_PENDING = frozenset(
    {
        "created",
        "waiting_for_resource",
        "preparing",
        "pending",
        "running",
        "scheduled",
        "manual",
    }
)

_JOB_BUCKETS: dict[str, str] = {
    "success": "pass",
    "failed": "fail",
    "canceled": "cancel",
    "skipped": "skipping",
    "manual": "skipping",
}

DOC_MERGE_STATUS_URL = "https://docs.gitlab.com/ee/api/merge_requests.html#merge-status"

DETAILED_MERGE_STATUS: dict[str, MergeRefusal | None] = {
    "mergeable": None,
    "unchecked": "pending",
    "checking": "pending",
    "preparing": "pending",
    "approvals_syncing": "pending",
    "ci_must_pass": "pending",
    "ci_still_running": "pending",
    "status_checks_must_pass": "pending",
    "external_status_checks": "pending",
    "security_policy_pipeline_check": "pending",
    "merge_time": "pending",
    "not_approved": "not_approved",
    "discussions_not_resolved": "discussions_not_resolved",
    "requested_changes": "requested_changes",
    "conflict": "conflict",
    "need_rebase": "conflict",
    "draft_status": "draft",
    "not_open": "not_open",
    "blocked_status": "blocked",
    "merge_request_blocked": "blocked",
    "policies_denied": "blocked",
    "security_policy_violations": "blocked",
    "jira_association_missing": "blocked",
    "commits_status": "blocked",
    "locked_paths": "blocked",
    "locked_lfs_files": "blocked",
    "title_regex": "blocked",
}
"""Every documented ``detailed_merge_status`` → the refusal it predicts (``None`` merges).

Source: :data:`DOC_MERGE_STATUS_URL`. Read before merging, because the
accept endpoint's refusal is a bare ``405 Method Not Allowed`` that
names no reason. Transient states (``unchecked``, ``checking``,
``preparing``, ``approvals_syncing``, pipelines and status checks still
running, ``merge_time``) are ``pending`` — recheck later. Only
``mergeable`` reaches the merge call; a value missing from this table
is ``unrecognized``, a named skip, never a blind merge attempt.
"""

_HEAD_MOVED_STATUS = "409"
"""Accept-MR status when ``sha`` no longer matches the source head (same doc)."""


def _username(payload: object) -> str:
    """Return ``payload["author"]["username"]`` or ``""``."""
    if not isinstance(payload, dict):
        return ""
    author = payload.get("author")
    if not isinstance(author, dict):
        return ""
    username = author.get("username")
    return username if isinstance(username, str) else ""


def _int(value: object) -> int | None:
    """Return ``value`` when it is an int (bools excluded), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def decode_concatenated(text: str) -> object:
    """Decode one JSON value, or several concatenated arrays merged into one.

    ``glab api --paginate`` prints each page as its own JSON array back
    to back, which :func:`json.loads` rejects as extra data.

    Raises:
        ValueError: The text is not JSON, or a page is not an array.
    """
    decoder = json.JSONDecoder()
    values: list[object] = []
    pos = 0
    stripped = text.strip()
    while pos < len(stripped):
        value, pos = decoder.raw_decode(stripped, pos)
        values.append(value)
        while pos < len(stripped) and stripped[pos].isspace():
            pos += 1
    if not values:
        return []
    if len(values) == 1:
        return values[0]
    merged: list[object] = []
    for value in values:
        if not isinstance(value, list):
            raise ValueError("expected every paginated page to be a JSON array")
        merged.extend(value)
    return merged


def pull_request_from_gitlab(mr: dict[str, object]) -> PullRequest:
    """Normalise a ``merge_requests/:iid`` payload into a :class:`PullRequest`."""
    return PullRequest(
        number=_int(mr.get("iid")) or 0,
        title=str(mr.get("title") or ""),
        body=str(mr.get("description") or ""),
        author=_username(mr),
        state=_STATE_TO_PROTOCOL.get(str(mr.get("state") or ""), "unknown"),
        head_sha=str(mr.get("sha") or ""),
        head_ref=str(mr.get("source_branch") or ""),
        base_ref=str(mr.get("target_branch") or ""),
        url=str(mr.get("web_url") or ""),
        draft=bool(mr.get("draft") or mr.get("work_in_progress")),
    )


def note_to_comment(
    note: dict[str, object],
    *,
    in_reply_to: int | None = None,
    thread_id: str | None = None,
) -> Comment:
    """Normalise a GitLab note into a :class:`Comment` (position → path/line)."""
    position = note.get("position")
    path: str | None = None
    line: int | None = None
    if isinstance(position, dict):
        raw_path = position.get("new_path") or position.get("old_path")
        path = raw_path if isinstance(raw_path, str) else None
        line = _int(position.get("new_line")) or _int(position.get("old_line"))
    return Comment(
        id=_int(note.get("id")) or 0,
        author=_username(note),
        body=str(note.get("body") or ""),
        path=path,
        line=line,
        created_at=str(note.get("created_at") or ""),
        in_reply_to=in_reply_to,
        thread_id=thread_id,
    )


def diff_notes_from_discussions(discussions: list[object]) -> list[Comment]:
    """Flatten MR discussions into inline comments (``DiffNote`` notes only)."""
    comments: list[Comment] = []
    for discussion in discussions:
        if not isinstance(discussion, dict):
            continue
        notes = discussion.get("notes")
        if not isinstance(notes, list):
            continue
        thread_id = discussion.get("id") if isinstance(discussion.get("id"), str) else None
        root_id: int | None = None
        for note in notes:
            if not isinstance(note, dict) or note.get("type") != "DiffNote":
                continue
            comment = note_to_comment(note, in_reply_to=root_id, thread_id=thread_id)
            comments.append(comment)
            if root_id is None:
                root_id = comment.id
    return comments


def render_diff_entry(entry: dict[str, object]) -> str:
    """Rebuild one ``merge_requests/:iid/diffs`` entry as a git-format file diff."""
    old_path = str(entry.get("old_path") or entry.get("new_path") or "")
    new_path = str(entry.get("new_path") or old_path)
    lines = [f"diff --git a/{old_path} b/{new_path}"]
    if entry.get("new_file"):
        lines.append(f"new file mode {entry.get('b_mode') or '100644'}")
    elif entry.get("deleted_file"):
        lines.append(f"deleted file mode {entry.get('a_mode') or '100644'}")
    elif entry.get("renamed_file"):
        lines += [f"rename from {old_path}", f"rename to {new_path}"]
    body = str(entry.get("diff") or "")
    if body:
        lines.append("--- /dev/null" if entry.get("new_file") else f"--- a/{old_path}")
        lines.append("+++ /dev/null" if entry.get("deleted_file") else f"+++ b/{new_path}")
        lines.append(body.rstrip("\n"))
    return "\n".join(lines) + "\n"


class GitLabForge:
    """The ``glab``-backed forge for GitLab merge requests."""

    name = "gitlab"

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        glab_path: str | None = None,
        timeout_s: int = 120,
        bot_username: str = "",
        project: str = PROJECT,
    ) -> None:
        """Initialise the forge.

        Args:
            cwd: The clone ``glab`` resolves ``:id`` from.
            glab_path: Override the ``glab`` binary location.
            timeout_s: Per-call timeout.
            bot_username: The username whose markers are trusted; empty
                trusts no marker.
            project: The project path prefix for REST calls; the
                ``projects/:id`` placeholder by default.
        """
        self._cwd = cwd or Path.cwd()
        self._glab = glab_path or shutil.which("glab")
        self._timeout = timeout_s
        self._bot_username = bot_username
        self._project = project

    @property
    def bot_username(self) -> str:
        """The username whose trust-bearing markers are honoured."""
        return self._bot_username

    def available(self) -> bool:
        """True when a ``glab`` binary is reachable."""
        return self._glab is not None and Path(self._glab).is_file()

    def is_trusted_author(self, comment: Comment) -> bool:
        """True when ``comment`` was authored by :attr:`bot_username` (fail-closed)."""
        return trusted(self._bot_username, comment.author)

    def _spawn(self, argv: list[str], *, input_data: str | None = None) -> CommandResult:
        """Run one subprocess in the clone; the seam the conformance suite replaces."""
        return spawn(argv, cwd=self._cwd, timeout_s=self._timeout, input_data=input_data)

    def _run(self, args: list[str], *, input_data: str | None = None) -> CommandResult:
        """Run ``glab`` with ``args``; ``returncode`` 127 when it is missing."""
        if not self.available():
            return CommandResult(
                returncode=127, stdout="", stderr="glab CLI not installed", argv=["glab", *args]
            )
        return self._spawn([str(self._glab), *args], input_data=input_data)

    def git(self, args: list[str]) -> CommandResult:
        """Run ``git`` in the clone."""
        git = shutil.which("git")
        if git is None:
            return CommandResult(
                returncode=127, stdout="", stderr="git not installed", argv=["git", *args]
            )
        return self._spawn([git, *args])

    @staticmethod
    def _raise(res: CommandResult, *, number: int | None = None) -> None:
        """Raise the :class:`ForgeError` matching a failed ``glab`` call."""
        message = (res.stderr or res.stdout).strip()[:500] or f"glab exited {res.returncode}"
        if res.returncode == 127:
            raise ForgeUnavailableError(message, binary="glab")
        status_match = _HTTP_STATUS_RE.search(res.stderr or "")
        status = status_match.group(1) if status_match else ""
        if status == "404":
            raise ForgeNotFoundError(message, number=number)
        if status in {"401", "403"}:
            raise ForgeForbiddenError(message)
        raise ForgeError(message)

    @staticmethod
    def _failed(res: CommandResult) -> bool:
        """True when ``glab`` failed — non-zero exit, or HTTP ≥ 400 on stderr."""
        if not res.ok:
            return True
        status_match = _HTTP_STATUS_RE.search(res.stderr or "")
        return status_match is not None and int(status_match.group(1)) >= 400

    def _api_text(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        paginate: bool = False,
        number: int | None = None,
    ) -> str:
        """Call ``glab api`` and return its raw stdout, raising on failure."""
        args = ["api"]
        if method != "GET":
            args += ["--method", method]
        if paginate:
            args.append("--paginate")
        args.append(endpoint)
        input_data: str | None = None
        if payload is not None:
            args += ["--header", "Content-Type: application/json", "--input", "-"]
            input_data = json.dumps(payload)
        res = self._run(args, input_data=input_data)
        if self._failed(res):
            self._raise(res, number=number)
        return res.stdout

    def _api(
        self,
        endpoint: str,
        *,
        method: str = "GET",
        payload: dict[str, object] | None = None,
        paginate: bool = False,
        number: int | None = None,
    ) -> object:
        """Call ``glab api`` and decode its JSON, raising on failure."""
        text = self._api_text(
            endpoint, method=method, payload=payload, paginate=paginate, number=number
        )
        try:
            return decode_concatenated(text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ForgeError(f"glab returned non-JSON for {endpoint}: {str(exc)[:160]}") from exc

    def _api_list(self, endpoint: str, *, number: int | None = None) -> list[dict[str, object]]:
        """GET a paginated array endpoint as a list of objects."""
        data = self._api(endpoint, paginate=True, number=number)
        if not isinstance(data, list):
            raise ForgeError(f"expected a JSON array from {endpoint}")
        return [entry for entry in data if isinstance(entry, dict)]

    def _mr(self, number: int) -> dict[str, object]:
        """GET ``merge_requests/:iid`` as a dict."""
        data = self._api(f"{self._project}/merge_requests/{number}", number=number)
        if not isinstance(data, dict) or "iid" not in data:
            raise ForgeNotFoundError(f"merge request !{number} not found", number=number)
        return data

    def pull_request(self, number: int) -> PullRequest:
        """Read one MR, normalised."""
        return pull_request_from_gitlab(self._mr(number))

    def pr_state(self, number: int) -> PullRequestState:
        """Read the MR state."""
        return self.pull_request(number).state

    def head_sha(self, number: int) -> str:
        """Read the MR head SHA.

        Raises:
            ForgeError: The MR reports no head.
        """
        sha = self.pull_request(number).head_sha
        if not sha:
            raise ForgeError(f"no head SHA reported for MR !{number}")
        return sha

    def diff(self, number: int) -> Diff:
        """Read the MR diff in git's format (``raw_diffs``, else rebuilt)."""
        try:
            text = self._api_text(
                f"{self._project}/merge_requests/{number}/raw_diffs", number=number
            )
        except ForgeNotFoundError:
            self._mr(number)
            _log.info("forge.gitlab.raw_diffs_unavailable", number=number)
            entries = self._api_list(
                f"{self._project}/merge_requests/{number}/diffs?per_page=100", number=number
            )
            text = "".join(render_diff_entry(entry) for entry in entries)
        return Diff(text=text, files=diff_files(text))

    def _discussions(self, number: int) -> list[dict[str, object]]:
        """GET every discussion of the MR."""
        return self._api_list(
            f"{self._project}/merge_requests/{number}/discussions?per_page=100", number=number
        )

    def comments(self, number: int) -> list[Comment]:
        """Read every inline (``DiffNote``) comment, roots and replies."""
        return diff_notes_from_discussions(list(self._discussions(number)))

    def conversation_comments(self, number: int) -> list[Comment]:
        """Read the non-system, non-diff notes, oldest first."""
        notes = self._api_list(
            f"{self._project}/merge_requests/{number}/notes"
            "?sort=asc&order_by=created_at&per_page=100",
            number=number,
        )
        return [
            note_to_comment(n) for n in notes if not n.get("system") and n.get("type") != "DiffNote"
        ]

    def reviews(self, number: int) -> list[Review]:
        """Read the MR approvals as ``APPROVED`` reviews."""
        data = self._api(f"{self._project}/merge_requests/{number}/approvals", number=number)
        approved = data.get("approved_by") if isinstance(data, dict) else None
        reviews: list[Review] = []
        for entry in approved or []:
            user = entry.get("user") if isinstance(entry, dict) else None
            if not isinstance(user, dict):
                continue
            username = user.get("username")
            reviews.append(
                Review(
                    id=_int(user.get("id")) or 0,
                    author=username if isinstance(username, str) else "",
                    state="APPROVED",
                )
            )
        return reviews

    def _post_note(self, number: int, body: str) -> Comment:
        """POST an unpositioned MR note."""
        data = self._api(
            f"{self._project}/merge_requests/{number}/notes",
            method="POST",
            payload={"body": body},
            number=number,
        )
        if not isinstance(data, dict):
            raise ForgeError("note creation returned a non-object payload")
        return note_to_comment(data)

    def _diff_refs(self, number: int) -> dict[str, str]:
        """The MR's ``diff_refs`` (``base_sha``/``start_sha``/``head_sha``)."""
        refs = self._mr(number).get("diff_refs")
        if not isinstance(refs, dict):
            raise ForgeError(f"merge request !{number} has no diff_refs")
        return {key: str(refs.get(key) or "") for key in ("base_sha", "start_sha", "head_sha")}

    def _post_positioned(
        self,
        number: int,
        body: str,
        *,
        path: str,
        line: int,
        refs: dict[str, str],
        commit_sha: str | None,
    ) -> Comment:
        """Open a diff discussion at ``path:line``; fall back to a plain note.

        A line the diff no longer has is refused by GitLab (HTTP 400);
        the comment is then posted unpositioned with ``path:line`` in its
        body and ``forge.position_fallback`` is logged.
        """
        position = {
            "position_type": "text",
            "base_sha": refs["base_sha"],
            "start_sha": refs["start_sha"],
            "head_sha": commit_sha or refs["head_sha"],
            "old_path": path,
            "new_path": path,
            "new_line": line,
        }
        try:
            data = self._api(
                f"{self._project}/merge_requests/{number}/discussions",
                method="POST",
                payload={"body": body, "position": position},
                number=number,
            )
        except (ForgeNotFoundError, ForgeForbiddenError):
            raise
        except ForgeError as exc:
            _log.warning(
                "forge.position_fallback",
                number=number,
                path=path,
                line=line,
                error=exc.message[:200],
            )
            return self._post_note(number, f"**`{path}:{line}`**\n\n{body}")
        notes = data.get("notes") if isinstance(data, dict) else None
        first = notes[0] if isinstance(notes, list) and notes else {}
        thread_id = data.get("id") if isinstance(data, dict) else None
        comment = note_to_comment(
            first if isinstance(first, dict) else {},
            thread_id=thread_id if isinstance(thread_id, str) else None,
        )
        if comment.path is None:
            comment = comment.model_copy(update={"path": path, "line": line})
        return comment

    @write_barrier
    def post_comment(
        self,
        number: int,
        body: str,
        *,
        path: str | None = None,
        line: int | None = None,
        commit_sha: str | None = None,
    ) -> Comment:
        """Post a note, or a positioned diff discussion when ``path``/``line`` are set."""
        if path is None or line is None:
            return self._post_note(number, body)
        return self._post_positioned(
            number,
            body,
            path=path,
            line=line,
            refs=self._diff_refs(number),
            commit_sha=commit_sha,
        )

    @write_barrier
    def post_review(self, number: int, review: ReviewDraft) -> Review:
        """Post one discussion per inline comment, then one verdict note.

        The MR is not approved (spec Q1). An inline comment that fails
        for a reason other than its position is logged and skipped so
        the verdict note still lands.

        Raises:
            ValueError: Inline comments were given without ``commit_sha``.
            ForgeError: The verdict note could not be posted.
        """
        if review.commit_sha is None and review.comments:
            raise ValueError("inline comments need a commit_sha to anchor to")
        posted: list[Comment] = []
        if review.comments:
            refs = self._diff_refs(number)
            for draft in review.comments:
                try:
                    posted.append(
                        self._post_positioned(
                            number,
                            draft.body,
                            path=draft.path,
                            line=draft.line,
                            refs=refs,
                            commit_sha=review.commit_sha,
                        )
                    )
                except ForgeError as exc:
                    _log.warning(
                        "forge.gitlab.inline_comment_failed",
                        number=number,
                        path=draft.path,
                        line=draft.line,
                        error=exc.message[:200],
                    )
        note = self._post_note(number, review.body)
        return Review(
            id=note.id,
            author=note.author,
            state=review.verdict.upper(),
            body=review.body,
            comments=posted,
        )

    @write_barrier
    def reply(self, number: int, comment_id: int, body: str) -> Comment:
        """Reply in the discussion holding note ``comment_id``.

        Raises:
            ForgeNotFoundError: No discussion holds that note.
        """
        for discussion in self._discussions(number):
            notes = discussion.get("notes")
            if not isinstance(notes, list):
                continue
            if not any(isinstance(n, dict) and n.get("id") == comment_id for n in notes):
                continue
            discussion_id = discussion.get("id")
            data = self._api(
                f"{self._project}/merge_requests/{number}/discussions/{discussion_id}/notes",
                method="POST",
                payload={"body": body},
                number=number,
            )
            if not isinstance(data, dict):
                raise ForgeError("reply creation returned a non-object payload")
            return note_to_comment(
                data,
                in_reply_to=comment_id,
                thread_id=discussion_id if isinstance(discussion_id, str) else None,
            )
        raise ForgeNotFoundError(f"no discussion holds note {comment_id}", number=number)

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        """Return the oldest trusted conversation note carrying ``marker``."""
        for comment in self.conversation_comments(number):
            if marker in comment.body and self.is_trusted_author(comment):
                return comment
        return None

    @write_barrier
    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        """Update the trusted marker note in place, or post it."""
        final_body = body if marker in body else f"{marker}\n{body}"
        existing = self.find_marked_comment(number, marker)
        if existing is None:
            return self._post_note(number, final_body)
        data = self._api(
            f"{self._project}/merge_requests/{number}/notes/{existing.id}",
            method="PUT",
            payload={"body": final_body},
            number=number,
        )
        if not isinstance(data, dict):
            raise ForgeError("note update returned a non-object payload")
        return note_to_comment(data)

    def ci_status(
        self,
        number: int,
        *,
        head_sha: str | None = None,
        required: list[str] | tuple[str, ...] | None = None,
    ) -> CheckStatus:
        """Judge ``head_pipeline.status`` — GitLab's one CI fact.

        ``required`` is ignored: a GitLab MR has one head pipeline. A
        pipeline built for another commit than ``head_sha`` counts as
        still pending (the head's own pipeline has not been reported yet).
        """
        mr = self._mr(number)
        pipeline = mr.get("head_pipeline")
        if not isinstance(pipeline, dict):
            return CheckStatus(state="missing", missing=["pipeline"], required=["pipeline"])
        status = str(pipeline.get("status") or "")
        run = CheckRun(
            name="pipeline",
            status=status.upper(),
            conclusion=status.upper() if status in _PIPELINE_SUCCESS | _PIPELINE_FAILURE else "",
            started_at=str(pipeline.get("started_at") or pipeline.get("created_at") or ""),
            link=str(pipeline.get("web_url") or ""),
        )
        pipeline_sha = str(pipeline.get("sha") or "")
        if head_sha and pipeline_sha and pipeline_sha != head_sha:
            return CheckStatus(
                state="pending", checks=[run], pending=["pipeline"], required=["pipeline"]
            )
        if status in _PIPELINE_SUCCESS:
            return CheckStatus(state="success", checks=[run], required=["pipeline"])
        if status in _PIPELINE_PENDING:
            return CheckStatus(
                state="pending", checks=[run], pending=["pipeline"], required=["pipeline"]
            )
        return CheckStatus(
            state="failure",
            checks=[run],
            failed=[f"pipeline={status.upper() or 'EMPTY'}"],
            required=["pipeline"],
        )

    def checks(self, number: int) -> list[CheckRun]:
        """Read the jobs of the MR's head pipeline (latest attempt of each)."""
        pipeline = self._mr(number).get("head_pipeline")
        pipeline_id = _int(pipeline.get("id")) if isinstance(pipeline, dict) else None
        if pipeline_id is None:
            return []
        jobs = self._api_list(
            f"{self._project}/pipelines/{pipeline_id}/jobs?per_page=100", number=number
        )
        runs: list[CheckRun] = []
        for job in jobs:
            status = str(job.get("status") or "")
            runs.append(
                CheckRun(
                    name=str(job.get("name") or ""),
                    status=status.upper(),
                    conclusion=status.upper() if status in _JOB_BUCKETS else "",
                    started_at=str(job.get("started_at") or ""),
                    bucket=_JOB_BUCKETS.get(status, "pending"),
                    link=str(job.get("web_url") or ""),
                    workflow=str(job.get("stage") or ""),
                )
            )
        return runs

    def check_log(self, link: str) -> str | None:
        """Read a job's trace from its ``/-/jobs/<id>`` link."""
        m = _JOB_LINK_RE.search(link)
        if m is None:
            return None
        return self._api_text(f"{self._project}/jobs/{m.group(1)}/trace")

    @staticmethod
    def merge_refusal(
        mr: dict[str, object], head_sha: str | None
    ) -> tuple[MergeRefusal, str] | None:
        """Name why GitLab would refuse to merge ``mr`` now, from its own fields.

        Order: a state other than ``opened``, a head that moved off
        ``head_sha``, then ``detailed_merge_status`` through
        :data:`DETAILED_MERGE_STATUS`, then a head pipeline still
        running. ``None`` only for ``mergeable`` with no running
        pipeline — the merge is then attempted.
        """
        state = _STATE_TO_PROTOCOL.get(str(mr.get("state") or ""), "unknown")
        if state != "open":
            return "not_open", f"merge request state is {mr.get('state')!r}"
        current = str(mr.get("sha") or "")
        if head_sha and current != head_sha:
            return "head_moved", f"head moved: expected {head_sha[:12]}, now {current[:12]}"
        detailed = str(mr.get("detailed_merge_status") or "")
        if detailed not in DETAILED_MERGE_STATUS:
            return "unrecognized", f"detailed_merge_status={detailed!r} is not a documented value"
        predicted = DETAILED_MERGE_STATUS[detailed]
        if predicted is not None:
            return predicted, f"detailed_merge_status={detailed}"
        pipeline = mr.get("head_pipeline")
        status = str(pipeline.get("status") or "") if isinstance(pipeline, dict) else ""
        if status in _PIPELINE_PENDING:
            return "pending", f"head_pipeline.status={status}"
        return None

    @write_barrier
    def merge(
        self,
        number: int,
        *,
        method: MergeMethod,
        delete_branch: bool,
        head_sha: str | None = None,
    ) -> MergeResult:
        """Merge with ``glab mr merge`` now (no auto-merge), pinned to the head.

        The MR is read first and :meth:`merge_refusal` decides from its
        fields; the merge is attempted only when nothing predicts a
        refusal, with ``--sha`` set to the head it was decided at so
        GitLab itself refuses (HTTP 409) a head that moved in between.

        Raises:
            ForgeMergeRefusedError: GitLab would refuse or refused; its
                ``reason`` comes from the MR's fields, ``head_moved`` for
                a 409, else ``refused``.
        """
        mr = self._mr(number)
        refusal = self.merge_refusal(mr, head_sha)
        if refusal is not None:
            raise ForgeMergeRefusedError(refusal[1], reason=refusal[0])
        pinned = head_sha or str(mr.get("sha") or "")
        args = ["mr", "merge", str(number), "--yes", "--auto-merge=false"]
        resolved = MergeMethod(method)
        if resolved is MergeMethod.SQUASH:
            args.append("--squash")
        elif resolved is MergeMethod.REBASE:
            args.append("--rebase")
        if delete_branch:
            args.append("--remove-source-branch")
        if pinned:
            args += ["--sha", pinned]
        res = self._run(args)
        if self._failed(res):
            if res.returncode == 127:
                self._raise(res, number=number)
            message = (res.stderr or res.stdout).strip()[-500:]
            moved = (
                f" {_HEAD_MOVED_STATUS} " in f" {message} "
                or f"HTTP {_HEAD_MOVED_STATUS}" in message
            )
            raise ForgeMergeRefusedError(message, reason="head_moved" if moved else "refused")
        mr = self._mr(number)
        sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha")
        return MergeResult(
            merged=mr.get("state") == "merged",
            sha=sha if isinstance(sha, str) else None,
            message=res.stdout[-500:],
        )

    def branch_protection(self, branch: str) -> BranchProtection:
        """Read the project's merge rules and the branch's push protection; never raises."""
        try:
            project = self._api(self._project)
        except ForgeError as exc:
            _log.info("forge.gitlab.protection_unreadable", branch=branch, error=exc.message[:200])
            return BranchProtection(readable=False)
        if not isinstance(project, dict):
            return BranchProtection(readable=False)
        allow_force = True
        try:
            protected = self._api(f"{self._project}/protected_branches/{branch}")
            if isinstance(protected, dict):
                allow_force = bool(protected.get("allow_force_push"))
        except ForgeNotFoundError:
            allow_force = True
        except ForgeError as exc:
            _log.info("forge.gitlab.protected_branch_unreadable", error=exc.message[:200])
            return BranchProtection(readable=False)
        merge_method = project.get("merge_method")
        return BranchProtection(
            required_checks=["pipeline"]
            if project.get("only_allow_merge_if_pipeline_succeeds")
            else [],
            allow_force_push=allow_force,
            readable=True,
            merge_method=merge_method if isinstance(merge_method, str) else None,
        )

    def open_pull_requests(self, base: str | None = None) -> list[PullRequest]:
        """List open MRs, optionally only those targeting ``base``."""
        query = "state=opened&per_page=100"
        if base:
            query += f"&target_branch={base}"
        entries = self._api_list(f"{self._project}/merge_requests?{query}")
        return [pull_request_from_gitlab(entry) for entry in entries]

    def merged_shas(self, base: str) -> set[str]:
        """Return the merge (and squash) commits merged MRs landed on ``base``."""
        entries = self._api_list(
            f"{self._project}/merge_requests?state=merged&target_branch={base}&per_page=100"
        )
        shas: set[str] = set()
        for entry in entries:
            for key in ("merge_commit_sha", "squash_commit_sha"):
                sha = entry.get(key)
                if isinstance(sha, str) and sha:
                    shas.add(sha)
        return shas

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Open an MR with ``glab mr create``; the URL is parsed from its output."""
        args = [
            "mr",
            "create",
            "--source-branch",
            head,
            "--target-branch",
            base,
            "--title",
            title,
            "--description",
            body,
            "--yes",
        ]
        if draft:
            args.append("--draft")
        res = self._run(args)
        if self._failed(res):
            self._raise(res)
        url = ""
        for line in reversed(res.stdout.strip().splitlines()):
            if "http" in line:
                url = line[line.index("http") :].strip()
                break
        m = _MR_URL_RE.search(url)
        return PullRequest(
            number=int(m.group(1)) if m else 0,
            title=title,
            body=body,
            author=self._bot_username,
            head_ref=head,
            base_ref=base,
            url=url,
            draft=draft,
        )

    def fetch_head(self, number: int) -> str:
        """Fetch ``refs/merge-requests/<iid>/head`` into the local object store."""
        ref = f"refs/merge-requests/{number}/head"
        res = self.git(["fetch", "--quiet", "origin", ref])
        if not res.ok:
            raise ForgeError(res.stderr.strip()[:200] or f"git fetch {ref} failed")
        return ref
