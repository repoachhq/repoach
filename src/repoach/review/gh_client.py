"""Thin wrapper around the ``gh`` CLI for review-bot team operations.

We deliberately use ``gh`` rather than the GitHub REST API directly:

1. ``gh`` handles auth via ``GH_TOKEN`` / ``GITHUB_TOKEN`` env transparently.
2. The same wrapper works locally and in GitHub Actions (where
   ``${{ secrets.GITHUB_TOKEN }}`` is auto-injected).
3. We don't need to maintain HTTPS retry / rate-limit logic.

All operations capture stdout/stderr so callers can audit what
happened.  The wrapper never raises on non-zero exit codes — it
returns a :class:`GhResult` and the caller decides.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger

_log = get_logger(__name__)


def flatten_paginated_pages(pages: object) -> list[object]:
    """Flatten a decoded ``gh api --paginate --slurp`` payload into one list.

    ``gh api --paginate`` alone emits one JSON array per page
    concatenated back to back (``[...][...]``), which ``json.loads``
    cannot parse past the first page. Adding ``--slurp`` wraps the
    per-page arrays into a single outer JSON array of page arrays —
    this helper flattens that shape into the full merged list.

    A single-page response ``--slurp``s to a one-element outer array
    and flattens back to that page's own list unchanged, so nominal
    single-page behaviour is preserved exactly.

    Args:
        pages: The value produced by ``json.loads`` on the raw
            ``--slurp``-wrapped stdout — expected to be a JSON array
            whose entries are themselves JSON arrays (one per page).

    Returns:
        The concatenation of every page's elements, in page order.

    Raises:
        ValueError: ``pages`` is not a JSON array of page arrays — a
            genuine parse failure that callers must surface loudly
            rather than collapse into an empty result.
    """
    if not isinstance(pages, list):
        raise ValueError("expected a JSON array of pages from --paginate --slurp")
    flattened: list[object] = []
    for page in pages:
        if not isinstance(page, list):
            raise ValueError("expected each paginated page to be a JSON array")
        flattened.extend(page)
    return flattened


def comment_author_login(comment: object) -> str | None:
    """Return a GitHub comment payload's ``user.login``, or ``None``.

    Handles the shapes :meth:`GhCli.list_review_comments` /
    :meth:`GhCli.find_archive_comment` actually see: a non-dict
    comment, a missing/null ``user`` field, or a missing/non-string
    ``login`` all resolve to ``None`` rather than raising — the
    SP-EVIDENCE-SENTINEL-AUTHOR author check treats every one of these
    as untrusted.
    """
    if not isinstance(comment, dict):
        return None
    user = comment.get("user")
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    return login if isinstance(login, str) and login else None


@dataclass(frozen=True)
class GhResult:
    """Result of a single ``gh`` invocation.

    Attributes:
        returncode: Exit code from the ``gh`` subprocess.
        stdout: Captured stdout (string).
        stderr: Captured stderr (string).
        argv: Argv list used; useful for audit logs.
    """

    returncode: int
    stdout: str
    stderr: str
    argv: list[str]

    @property
    def ok(self) -> bool:
        """True when the command exited with code 0."""
        return self.returncode == 0


@dataclass(frozen=True)
class ArchiveFetch:
    """Outcome of an archive-comment fetch (SP-SAFE-MERGE-ARCHIVE-RETRY).

    Distinguishes the three cases that
    :meth:`GhCli.fetch_archive_comment` historically collapsed into a
    single ``None``: the comment exists (``body`` set), the comment is
    genuinely absent (both fields ``None``), and the GitHub API call
    itself failed (``api_error`` set) — e.g. a secondary-rate-limit 403
    right after the review team's posting burst.  Callers gating on the
    archive (``safe_merge.sh`` step [5/6]) need the distinction to
    retry transient API failures instead of reporting a missing
    archive.

    Attributes:
        body: Raw markdown body of the archive comment, when found.
        api_error: Short description of the API failure, when the
            fetch itself failed; ``None`` on a clean fetch.
    """

    body: str | None
    api_error: str | None


class GhCli:
    """Tiny wrapper around the ``gh`` binary for our review use cases."""

    def __init__(
        self,
        *,
        cwd: Path | None = None,
        gh_path: str | None = None,
        timeout_s: int = 60,
        bot_login: str | None = None,
    ) -> None:
        """Initialise the wrapper.

        Args:
            cwd: Working directory for ``gh`` invocations (must contain
                a git repo whose origin is the GitHub repo).
            gh_path: Override the ``gh`` binary location.  When
                ``None``, look up via ``shutil.which``.
            timeout_s: Per-call timeout.
            bot_login: The GitHub login trust-bearing markers must be
                authored by to be honoured (SP-EVIDENCE-SENTINEL-AUTHOR).
                Defaults to :attr:`~repoach.core.config.Settings.review_bot_login`
                when omitted, so production call sites need no change.
        """
        self._cwd = cwd or Path.cwd()
        self._gh = gh_path or shutil.which("gh") or shutil.which(str(Path.home() / ".local/bin/gh"))
        self._timeout = timeout_s
        self._bot_login = bot_login if bot_login is not None else get_settings().review_bot_login

    @property
    def available(self) -> bool:
        """True if a ``gh`` binary is reachable."""
        return self._gh is not None and Path(self._gh).is_file()

    @property
    def bot_login(self) -> str:
        """The GitHub login trust-bearing markers must be authored by."""
        return self._bot_login

    def _is_trusted_author(self, comment: object) -> bool:
        """True when ``comment`` was authored by :attr:`bot_login`.

        Fails closed: an empty/unresolvable :attr:`bot_login` never
        matches, and a missing/null author never matches — a missing
        identity must never widen trust (SP-EVIDENCE-SENTINEL-AUTHOR).
        """
        return bool(self._bot_login) and comment_author_login(comment) == self._bot_login

    def _spawn(self, argv: list[str], *, input_data: str | None = None) -> GhResult:
        """Run a subprocess in ``cwd`` and capture output as a GhResult.

        Args:
            argv: Full argv to spawn.
            input_data: Optional stdin payload (SP-REVIEW-POST-BATCH) —
                forwarded verbatim to ``subprocess.run``'s own ``input``
                kwarg. ``None`` (the default) reproduces every call
                site's prior behavior exactly, since that is already
                ``subprocess.run``'s own default for ``input``.
        """
        try:
            proc = subprocess.run(
                argv,
                cwd=self._cwd,
                capture_output=True,
                text=True,
                timeout=self._timeout,
                check=False,
                input=input_data,
            )
            return GhResult(
                returncode=proc.returncode,
                stdout=proc.stdout,
                stderr=proc.stderr,
                argv=argv,
            )
        except subprocess.TimeoutExpired as exc:
            return GhResult(
                returncode=-1,
                stdout=exc.stdout or "",
                stderr=f"timeout after {self._timeout}s",
                argv=argv,
            )

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        """Spawn ``gh`` with the given subargs and capture output.

        Args:
            args: Subargs appended after the ``gh`` binary path.
            input_data: Optional stdin payload, forwarded to
                :meth:`_spawn` (SP-REVIEW-POST-BATCH) — needed by
                :meth:`pr_review_submit_batch`, the first caller in this
                file whose payload (a nested ``comments`` array) cannot
                be expressed through the scalar ``-f``/``-F`` flags used
                everywhere else.
        """
        if not self.available:
            return GhResult(
                returncode=127,
                stdout="",
                stderr="gh CLI not installed",
                argv=["gh", *args],
            )
        return self._spawn([str(self._gh), *args], input_data=input_data)

    def _run_git(self, args: list[str]) -> GhResult:
        """Spawn ``git`` in ``cwd`` and capture output like :meth:`_run`."""
        git = shutil.which("git")
        if git is None:
            return GhResult(
                returncode=127,
                stdout="",
                stderr="git not installed",
                argv=["git", *args],
            )
        return self._spawn([git, *args])

    def pr_diff(self, pr_number: int) -> GhResult:
        """Fetch the unified diff of a PR.

        Tries ``gh pr diff`` first.  GitHub's diff media type refuses
        any PR touching more than 300 files with HTTP 406 — the
        Genesis Reset PR (#309, 731 files) hit exactly that wall and
        the review team degraded to a blind ``COMMENT`` verdict.  On
        any ``gh pr diff`` failure we therefore fall back to computing
        the same three-dot diff locally with ``git`` (see
        :meth:`_pr_diff_via_local_git`).

        Args:
            pr_number: GitHub PR number on origin.

        Returns:
            :class:`GhResult` whose ``stdout`` contains the diff.
        """
        res = self._run(["pr", "diff", str(pr_number)])
        if res.ok:
            return res
        _log.warning(
            "gh_client.pr_diff_api_failed",
            pr_number=pr_number,
            stderr=res.stderr[:200],
        )
        return self._pr_diff_via_local_git(pr_number, api_result=res)

    def _pr_diff_via_local_git(self, pr_number: int, *, api_result: GhResult) -> GhResult:
        """Compute a PR's diff with local git when the API path fails.

        Resolves the PR's base branch and head SHA via ``gh pr view``,
        fetches the base's tip explicitly into its remote-tracking
        ref, fetches ``refs/pull/<N>/head`` when the head commit is
        not already in the object store (the GitHub Actions checkout
        usually has it), then runs ``git diff origin/<base>...<head>``
        — the same merge-base semantics GitHub renders.  Requires
        ``cwd`` to be a clone of the repo, which is already a
        documented invariant of this class.

        Args:
            pr_number: PR number.
            api_result: The failed ``gh pr diff`` result, returned
                unchanged when the fallback cannot run either so the
                caller sees the original API error.

        Returns:
            :class:`GhResult` with the diff in ``stdout`` on success,
            otherwise ``api_result``.
        """
        view = self._run(["pr", "view", str(pr_number), "--json", "baseRefName,headRefOid"])
        if not view.ok:
            return api_result
        try:
            meta = json.loads(view.stdout)
        except json.JSONDecodeError as exc:
            _log.warning(
                "gh_client.pr_diff_fallback_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return api_result
        base = meta.get("baseRefName")
        head = meta.get("headRefOid")
        if not isinstance(base, str) or not base or not isinstance(head, str) or not head:
            return api_result
        fetch_base = self._run_git(
            ["fetch", "--quiet", "origin", f"+refs/heads/{base}:refs/remotes/origin/{base}"]
        )
        if not fetch_base.ok:
            _log.warning(
                "gh_client.pr_diff_fallback_fetch_base_failed",
                pr_number=pr_number,
                base=base,
                stderr=fetch_base.stderr[:200],
            )
            return api_result
        head_probe = ["cat-file", "-e", f"{head}^{{commit}}"]
        if not self._run_git(head_probe).ok:
            self._run_git(["fetch", "--quiet", "origin", f"refs/pull/{pr_number}/head"])
            if not self._run_git(head_probe).ok:
                _log.warning(
                    "gh_client.pr_diff_fallback_head_unavailable",
                    pr_number=pr_number,
                    head=head,
                )
                return api_result
        diff = self._run_git(["diff", f"origin/{base}...{head}"])
        if not diff.ok:
            _log.error(
                "gh_client.pr_diff_fallback_diff_failed",
                pr_number=pr_number,
                stderr=diff.stderr[:200],
            )
            return api_result
        _log.info(
            "gh_client.pr_diff_fallback_local_git",
            pr_number=pr_number,
            base=base,
            head=head,
            diff_chars=len(diff.stdout),
        )
        return diff

    def pr_view(self, pr_number: int) -> dict[str, object] | None:
        """Fetch PR metadata as a JSON dict.

        Args:
            pr_number: GitHub PR number on origin.

        Returns:
            Parsed JSON dict or ``None`` on failure.
        """
        res = self._run(
            [
                "pr",
                "view",
                str(pr_number),
                "--json",
                "number,title,body,headRefName,baseRefName,author,state,url",
            ]
        )
        if not res.ok:
            return None
        try:
            return json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            _log.warning(
                "gh_client.pr_view_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return None

    def pr_create(
        self,
        *,
        head: str,
        base: str | None = None,
        title: str,
        body: str,
        draft: bool = False,
    ) -> GhResult:
        """Open a new pull request.

        Args:
            head: Source branch name (without ``origin/`` prefix).
            base: Target branch. Defaults to
                :attr:`~repoach.core.config.Settings.release_branch`,
                resolved at call time so an env/test override takes effect.
            title: PR title.
            body: PR body (markdown).
            draft: When True, create a draft PR.

        Returns:
            :class:`GhResult` whose ``stdout`` ends with the PR URL on
            success.
        """
        base = base if base is not None else get_settings().release_branch
        args = [
            "pr",
            "create",
            "--head",
            head,
            "--base",
            base,
            "--title",
            title,
            "--body",
            body,
        ]
        if draft:
            args.append("--draft")
        return self._run(args)

    def pr_review_comment(
        self,
        pr_number: int,
        *,
        body: str,
        commit_sha: str | None = None,
        file: str | None = None,
        line: int | None = None,
    ) -> GhResult:
        """Post a single review comment on a PR.

        ``gh`` does not expose a clean wrapper for inline review
        comments today, so we go through the REST API.  The wrapper
        favours a robustness-over-elegance approach: when ``file``
        and ``line`` are omitted, the comment lands on the PR
        conversation tab as a regular issue comment.

        Args:
            pr_number: PR number.
            body: Comment markdown.
            commit_sha: Optional commit SHA for inline comments.
            file: Optional filename for inline comments.
            line: Optional 1-based line number.

        Returns:
            :class:`GhResult`.
        """
        if file is None or line is None or commit_sha is None:
            return self._run(["pr", "comment", str(pr_number), "--body", body])
        return self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/:owner/:repo/pulls/{pr_number}/comments",
                "-f",
                f"body={body}",
                "-f",
                f"commit_id={commit_sha}",
                "-f",
                f"path={file}",
                "-F",
                f"line={line}",
                "-f",
                "side=RIGHT",
            ]
        )

    def pr_review_submit(
        self,
        pr_number: int,
        *,
        verdict: str,
        summary: str,
    ) -> GhResult:
        """Submit a final review verdict (APPROVE / REQUEST_CHANGES / COMMENT).

        Args:
            pr_number: PR number.
            verdict: One of "APPROVE" / "REQUEST_CHANGES" / "COMMENT".
            summary: Review body.

        Returns:
            :class:`GhResult`.
        """
        flag_map = {
            "APPROVE": "--approve",
            "REQUEST_CHANGES": "--request-changes",
            "COMMENT": "--comment",
        }
        flag = flag_map.get(verdict.upper(), "--comment")
        return self._run(
            [
                "pr",
                "review",
                str(pr_number),
                flag,
                "--body",
                summary,
            ]
        )

    def pr_review_submit_batch(
        self,
        pr_number: int,
        *,
        commit_sha: str,
        verdict: str,
        body: str,
        comments: list[dict[str, object]],
    ) -> GhResult:
        """Submit a verdict and every inline comment as one review.

        Wraps ``POST /repos/{owner}/{repo}/pulls/{pull_number}/reviews``
        — the batched-review endpoint (SP-REVIEW-POST-BATCH) that
        replaces one ``gh`` subprocess per finding with a single call
        carrying the whole reviewer's output. The scalar ``-f``/``-F``
        flags used by :meth:`pr_review_comment` and
        :meth:`pr_review_submit` cannot express a nested array of
        comment objects, so the payload is fed as JSON over stdin via
        ``--input -`` instead.

        Args:
            pr_number: PR number.
            commit_sha: Commit SHA the inline comments anchor to
                (``commit_id`` in GitHub's payload).
            verdict: One of "APPROVE" / "REQUEST_CHANGES" / "COMMENT";
                forwarded verbatim as the review's ``event``. An
                unrecognised value falls back to "COMMENT", matching
                :meth:`pr_review_submit`'s existing
                ``flag_map.get(..., "--comment")`` fallback style.
            body: Review body markdown.
            comments: One dict per inline comment, each carrying
                ``"file"`` (repo-relative path), ``"line"`` (1-based),
                and ``"body"`` (already-rendered comment markdown —
                this method does not add any role/severity prefix).

        Returns:
            :class:`GhResult`; on success ``stdout`` carries the
            created review's JSON, including its own ``"id"``.
        """
        flag_map = {
            "APPROVE": "APPROVE",
            "REQUEST_CHANGES": "REQUEST_CHANGES",
            "COMMENT": "COMMENT",
        }
        event = flag_map.get(verdict.upper(), "COMMENT")
        payload = {
            "commit_id": commit_sha,
            "event": event,
            "body": body,
            "comments": [
                {
                    "path": comment["file"],
                    "line": comment["line"],
                    "side": "RIGHT",
                    "body": comment["body"],
                }
                for comment in comments
            ],
        }
        return self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/:owner/:repo/pulls/{pr_number}/reviews",
                "--input",
                "-",
            ],
            input_data=json.dumps(payload),
        )

    def list_review_id_comments(self, pr_number: int, review_id: int) -> list[dict[str, object]]:
        """Return the inline comments belonging to one specific review.

        Wraps ``GET /repos/.../pulls/{pr}/reviews/{review_id}/comments``
        — scoped to a single review's own comments, unlike
        :meth:`list_review_comments`'s whole-PR scope. Lets a caller
        recover just the ids of the comments a
        :meth:`pr_review_submit_batch` call created
        (SP-REVIEW-POST-BATCH). Follows the same single-page
        ``json.loads`` convention as :meth:`list_review_comments` and
        :meth:`find_archive_comment` — the shared multi-page
        ``--paginate`` JSON-decode limitation is a pre-existing gap
        this method inherits rather than fixes.

        Args:
            pr_number: PR number.
            review_id: The ``id`` of a review previously created by
                :meth:`pr_review_submit_batch`.

        Returns:
            JSON-decoded list, each entry carrying at least ``"id"``,
            ``"path"``, ``"line"``; ``[]`` on any failure.
        """
        res = self._run(
            [
                "api",
                f"repos/:owner/:repo/pulls/{pr_number}/reviews/{review_id}/comments",
            ]
        )
        if not res.ok:
            return []
        try:
            data = json.loads(res.stdout) if res.stdout.strip() else []
        except json.JSONDecodeError as exc:
            _log.warning(
                "gh_client.review_id_comments_decode_failed",
                pr_number=pr_number,
                review_id=review_id,
                error=str(exc)[:200],
            )
            return []
        if not isinstance(data, list):
            return []
        return data

    def list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        """Return every inline review comment on a PR (root + replies).

        Wraps ``GET /repos/.../pulls/{pr}/comments`` with paging.  The
        response shape is the GitHub REST default — each entry carries
        ``id``, ``in_reply_to_id`` (None for root threads), ``path``,
        ``line``, ``body``, ``user.login``.

        Returns:
            JSON-decoded list, or ``[]`` on any failure (paired with a
            structured log so the caller can fail closed without
            blowing up).
        """
        res = self._run(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/:owner/:repo/pulls/{pr_number}/comments",
            ]
        )
        if not res.ok:
            return []
        try:
            pages = json.loads(res.stdout) if res.stdout.strip() else []
            data = flatten_paginated_pages(pages)
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "gh_client.review_comments_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return []
        return data

    def reply_to_review_comment(
        self,
        pr_number: int,
        *,
        comment_id: int,
        body: str,
    ) -> GhResult:
        """Post a threaded reply on an existing inline review comment.

        Wraps ``POST /repos/.../pulls/{pr}/comments/{id}/replies`` —
        the only GitHub endpoint that nests a comment inside an
        existing review thread.  ``gh pr review --comment`` cannot do
        this; we go through the REST API.

        Args:
            pr_number: PR number.
            comment_id: ID of the **root** comment to reply to.
            body: Markdown reply body.

        Returns:
            :class:`GhResult`.
        """
        return self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/:owner/:repo/pulls/{pr_number}/comments/{comment_id}/replies",
                "-f",
                f"body={body}",
            ]
        )

    def pr_head_sha(self, pr_number: int) -> str | None:
        """Return the SHA of the PR's head commit, or ``None`` on error."""
        res = self._run(["pr", "view", str(pr_number), "--json", "headRefOid"])
        if not res.ok:
            return None
        try:
            data = json.loads(res.stdout)
            return data.get("headRefOid")
        except json.JSONDecodeError as exc:
            _log.warning(
                "gh_client.pr_head_sha_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return None

    ARCHIVE_MARKER = "<!-- repoach-review-archive -->"

    def find_archive_comment(self, pr_number: int) -> int | None:
        """Return the id of the existing archive comment on a PR, if any.

        Walks the PR's issue-comments and matches on the marker
        :attr:`ARCHIVE_MARKER`.  Returns ``None`` if no comment carries
        the marker, no marker-bearing comment was authored by
        :attr:`bot_login` (SP-EVIDENCE-SENTINEL-AUTHOR — a forged
        marker from any other author is ignored), or the API call
        fails.
        """
        res = self._run(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/:owner/:repo/issues/{pr_number}/comments",
            ]
        )
        if not res.ok:
            _log.warning(
                "gh_client.find_archive_api_failed",
                pr_number=pr_number,
                returncode=res.returncode,
                error=res.stderr.strip()[:200],
            )
            return None
        try:
            comments = flatten_paginated_pages(json.loads(res.stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "gh_client.find_archive_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return None
        for c in comments:
            if not isinstance(c, dict) or self.ARCHIVE_MARKER not in (c.get("body") or ""):
                continue
            if not self._is_trusted_author(c):
                continue
            cid = c.get("id")
            if isinstance(cid, int):
                return cid
        return None

    def upsert_archive_comment(self, pr_number: int, *, body: str) -> GhResult:
        """Create or update the sticky archive comment on a PR.

        The comment is identified by :attr:`ARCHIVE_MARKER` (an HTML
        comment that GitHub renders invisibly).  When found, we PATCH
        it; otherwise we POST a new issue comment.

        Args:
            pr_number: PR number.
            body: Markdown body — must already contain the marker
                (callers should prepend it).

        Returns:
            :class:`GhResult`.
        """
        if self.ARCHIVE_MARKER not in body:
            body = f"{self.ARCHIVE_MARKER}\n{body}"
        existing = self.find_archive_comment(pr_number)
        if existing is None:
            return self._run(
                [
                    "api",
                    "--method",
                    "POST",
                    f"repos/:owner/:repo/issues/{pr_number}/comments",
                    "-f",
                    f"body={body}",
                ]
            )
        return self._run(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/:owner/:repo/issues/comments/{existing}",
                "-f",
                f"body={body}",
            ]
        )

    def fetch_archive_comment(self, pr_number: int) -> str | None:
        """Return the raw body of the archive comment on a PR (or ``None``).

        Collapses API failure and genuine absence into ``None`` — use
        :meth:`fetch_archive_comment_with_status` when the caller must
        tell the two apart.
        """
        return self.fetch_archive_comment_with_status(pr_number).body

    def fetch_archive_comment_with_status(self, pr_number: int) -> ArchiveFetch:
        """Fetch the archive comment, distinguishing absence from API failure.

        Sibling of :meth:`fetch_archive_comment`
        (SP-SAFE-MERGE-ARCHIVE-RETRY): the legacy method silently
        returned ``None`` when the ``gh api`` call itself failed, which
        downstream consumers misread as "no archive comment on the PR"
        — masking transient GitHub failures (secondary rate limit
        after the review team's posting burst) behind a misleading
        message.

        Args:
            pr_number: PR number.

        Returns:
            :class:`ArchiveFetch` — ``body`` set when a comment
            carrying :attr:`ARCHIVE_MARKER` **and** authored by
            :attr:`bot_login` was found, ``api_error`` set when the
            fetch failed, both ``None`` when the API succeeded but no
            trusted archive comment exists (including a forged
            marker-bearing comment from another author —
            SP-EVIDENCE-SENTINEL-AUTHOR).
        """
        res = self._run(
            [
                "api",
                "--paginate",
                "--slurp",
                f"repos/:owner/:repo/issues/{pr_number}/comments",
            ]
        )
        if not res.ok:
            error = res.stderr.strip()[:200] or f"gh exited {res.returncode}"
            _log.warning(
                "gh_client.archive_fetch_api_failed",
                pr_number=pr_number,
                returncode=res.returncode,
                error=error,
            )
            return ArchiveFetch(body=None, api_error=error)
        try:
            comments = flatten_paginated_pages(json.loads(res.stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            _log.warning(
                "gh_client.archive_body_decode_failed",
                pr_number=pr_number,
                error=str(exc)[:200],
            )
            return ArchiveFetch(body=None, api_error=f"JSON decode failed: {str(exc)[:160]}")
        for c in comments:
            comment = c if isinstance(c, dict) else {}
            body = comment.get("body") or ""
            if self.ARCHIVE_MARKER in body and self._is_trusted_author(comment):
                return ArchiveFetch(body=body, api_error=None)
        return ArchiveFetch(body=None, api_error=None)

    def list_open_prs(self) -> list[dict[str, object]]:
        """Return every open PR on the repo as a list of metadata dicts."""
        res = self._run(
            [
                "pr",
                "list",
                "--state",
                "open",
                "--json",
                "number,title,headRefName,baseRefName,author,url",
                "--limit",
                "100",
            ]
        )
        if not res.ok:
            _log.warning("gh.pr_list_failed", stderr=res.stderr[:200])
            return []
        try:
            return json.loads(res.stdout) or []
        except json.JSONDecodeError as exc:
            _log.warning("gh_client.list_open_prs_decode_failed", error=str(exc)[:200])
            return []
