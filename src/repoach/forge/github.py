"""GitHub implementation of the :class:`~repoach.forge.protocol.Forge` protocol.

Formerly ``repoach.review.gh_client`` (SP-UNIVERSAL-FORGE G2). We
deliberately use ``gh`` rather than the GitHub REST API directly:

1. ``gh`` handles auth via ``GH_TOKEN`` / ``GITHUB_TOKEN`` env transparently.
2. The same wrapper works locally and in GitHub Actions (where
   ``${{ secrets.GITHUB_TOKEN }}`` is auto-injected).
3. We don't need to maintain HTTPS retry / rate-limit logic.

The class keeps two layers. The *transport* layer is the historical
``gh`` wrapper, method for method (``pr_diff``, ``pr_view``,
``pr_review_submit_batch`` …): each returns a :class:`GhResult` or a
decoded payload and never raises, exactly as before, so the argv sent
to ``gh`` for repoach's own repository is byte-identical. The
*protocol* layer (``pull_request``, ``diff``, ``post_review``,
``ci_status``, ``merge`` …) is built on the transport layer, normalises
the payloads into :mod:`repoach.forge.protocol` models and raises a
:class:`~repoach.forge.protocol.ForgeError` instead of returning a
failed result. Callers outside :mod:`repoach.forge` use the protocol
layer only.
"""

from __future__ import annotations

import json
import re
import shutil
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from ..core.config import get_settings
from ..core.logging import get_logger
from .protocol import (
    ARCHIVE_MARKER,
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

GhResult = CommandResult
"""Historical name of :class:`~repoach.forge.runner.CommandResult`."""

DEFAULT_REQUIRED_CHECK_NAMES: tuple[str, ...] = (
    "Test suite (Python 3.11)",
    "Test suite (Python 3.13)",
)
"""The configured fallback of required check names (SP-CI-CHECKNAME-SYNC).

Mirrors the matrix in ``.github/workflows/ci.yml``; used whenever the
branch protection cannot be read or names no required check.
"""

_FAIL_CONCLUSIONS: frozenset[str] = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
_SUCCESS_CONCLUSIONS: frozenset[str] = frozenset({"SUCCESS", "NEUTRAL", "SKIPPED"})
_PENDING_STATUSES: frozenset[str] = frozenset(
    {"QUEUED", "IN_PROGRESS", "PENDING", "WAITING", "EXPECTED", "REQUESTED"}
)

_MERGE_SHA_RE = re.compile(r"\b([0-9a-f]{40})\b")
_RUN_LINK_RE = re.compile(r"/runs/(\d+)(?:/job/(\d+))?")
_PULL_URL_RE = re.compile(r"/pull/(\d+)")
_HTTP_STATUS_RE = re.compile(r"HTTP (\d{3})")

_GH_STATE_TO_PROTOCOL: dict[str, PullRequestState] = {
    "OPEN": "open",
    "MERGED": "merged",
    "CLOSED": "closed",
}
"""``gh`` states; anything else maps to ``unknown`` (refuses writes)."""

MERGEABILITY_FIELDS = "state,isDraft,mergeable,mergeStateStatus,headRefOid,statusCheckRollup"
"""``gh pr view --json`` fields :meth:`GitHubForge.merge` reads before merging."""

MERGE_STATE_STATUS: dict[str, MergeRefusal | None] = {
    "CLEAN": None,
    "HAS_HOOKS": None,
    "UNSTABLE": None,
    "BLOCKED": "blocked",
    "BEHIND": "behind",
    "DIRTY": "conflict",
    "DRAFT": "draft",
    "UNKNOWN": "refused",
}
"""Every documented ``mergeStateStatus`` → the refusal it predicts (``None`` merges).

https://docs.github.com/en/graphql/reference/enums#mergestatestatus —
``UNSTABLE`` (non-passing non-required checks) still merges; ``UNKNOWN``
twice in a row is refused (FAILED).
"""

_WAITS_ON_CHECKS = frozenset({"BLOCKED", "BEHIND", "UNSTABLE"})


def _rollup_pending(rollup: object) -> bool:
    """True when a ``statusCheckRollup`` entry is still queued or running."""
    if not isinstance(rollup, list):
        return False
    for entry in rollup:
        if not isinstance(entry, dict):
            continue
        status = str(entry.get("status") or "").upper()
        context_state = str(entry.get("state") or "").upper()
        if status in _PENDING_STATUSES or context_state in _PENDING_STATUSES:
            return True
    return False


_MERGE_METHOD_FLAGS: dict[MergeMethod, str] = {
    MergeMethod.SQUASH: "--squash",
    MergeMethod.MERGE: "--merge",
    MergeMethod.REBASE: "--rebase",
}


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

    Handles the shapes the comment listings actually see: a non-dict
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


def classify_required_checks(
    rollup: Iterable[dict],
    required_names: Sequence[str] = DEFAULT_REQUIRED_CHECK_NAMES,
) -> tuple[list[str], list[str], list[str]]:
    """Bucket required checks into ``(failed, pending, missing)``.

    Each ``rollup`` entry is a ``{"name", "status", "conclusion", ...}``
    dict, from either ``gh pr view --json statusCheckRollup`` or the
    commit's check-runs. Duplicate entries (workflow re-runs,
    concurrency cancellations) collapse to the latest ``startedAt``
    per name — neither source is reliably ordered, and the observed
    stale-cancelled-over-fresh-green pinning (PR #3, 2026-07-03) means
    iteration order must never decide.  ISO-8601 timestamps compare
    lexicographically; an entry without one is treated as oldest.  An
    unknown conclusion bucket is treated as a failure (fail-closed).
    """
    by_name: dict[str, dict] = {}
    for entry in rollup:
        name = str(entry.get("name") or "")
        if not name:
            continue
        started = str(entry.get("startedAt") or "")
        current = by_name.get(name)
        if current is None or started >= str(current.get("startedAt") or ""):
            by_name[name] = dict(entry)

    failed: list[str] = []
    pending: list[str] = []
    missing: list[str] = []
    for required in required_names:
        match = by_name.get(required)
        if match is None:
            missing.append(required)
            continue
        conclusion = str(match.get("conclusion") or "").upper()
        status = str(match.get("status") or "").upper()
        if conclusion in _FAIL_CONCLUSIONS:
            failed.append(f"{required}={conclusion}")
        elif conclusion in _SUCCESS_CONCLUSIONS:
            continue
        elif status in _PENDING_STATUSES or not conclusion:
            pending.append(required)
        else:
            failed.append(f"{required}={conclusion or 'EMPTY'}")
    return failed, pending, missing


def check_state(failed: Sequence[str], pending: Sequence[str], missing: Sequence[str]) -> str:
    """Fold the three required-check buckets into one :class:`CheckStatus` state.

    Precedence matches the auto-merge gate: any failure wins; missing
    checks with nothing pending are ``missing``; nothing pending and
    nothing missing is ``success``; anything else is still ``pending``.
    """
    if failed:
        return "failure"
    if missing and not pending:
        return "missing"
    if not pending and not missing:
        return "success"
    return "pending"


def _json_id(stdout: str) -> int | None:
    """Extract the integer top-level ``id`` of a ``gh api`` JSON response.

    Tolerant of empty / non-JSON output (returns ``None``) so a missing
    id never crashes a posting pass.
    """
    if not stdout:
        return None
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        _log.debug("forge.github.id_json_decode_failed", error=str(exc)[:120])
        return None
    if not isinstance(data, dict):
        return None
    raw = data.get("id")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        _log.debug("forge.github.id_not_int", raw=str(raw)[:50])
        return None


def _int_or_none(value: object) -> int | None:
    """Return ``value`` as an int when it is one (bools excluded), else ``None``."""
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


def comment_from_github(payload: dict[str, object]) -> Comment:
    """Normalise a GitHub issue / review comment payload into a :class:`Comment`."""
    return Comment(
        id=_int_or_none(payload.get("id")) or 0,
        author=comment_author_login(payload) or "",
        body=str(payload.get("body") or ""),
        path=payload.get("path") if isinstance(payload.get("path"), str) else None,
        line=_int_or_none(payload.get("line")),
        created_at=str(payload.get("created_at") or ""),
        in_reply_to=_int_or_none(payload.get("in_reply_to_id")),
    )


def pull_request_from_github(payload: dict[str, object], *, number: int) -> PullRequest:
    """Normalise a ``gh pr view/list --json`` payload into a :class:`PullRequest`.

    Missing fields default to empty values — the historical callers
    read every field with ``.get(...) or ""`` and keep doing so here.
    """
    author = payload.get("author")
    login = author.get("login") if isinstance(author, dict) else None
    raw_state = str(payload.get("state") or "OPEN").upper()
    raw_number = _int_or_none(payload.get("number"))
    return PullRequest(
        number=raw_number if raw_number is not None else number,
        title=str(payload.get("title") or ""),
        body=str(payload.get("body") or ""),
        author=login if isinstance(login, str) else "",
        state=_GH_STATE_TO_PROTOCOL.get(raw_state, "unknown"),
        head_sha=str(payload.get("headRefOid") or ""),
        head_ref=str(payload.get("headRefName") or ""),
        base_ref=str(payload.get("baseRefName") or ""),
        url=str(payload.get("url") or ""),
        draft=bool(payload.get("isDraft")),
    )


def raise_for_gh(res: CommandResult, *, number: int | None = None) -> None:
    """Raise the :class:`ForgeError` subclass matching a failed ``gh`` call.

    Args:
        res: The failed result.
        number: The PR number addressed, carried by a not-found error.

    Raises:
        ForgeUnavailableError: ``gh`` is not installed.
        ForgeNotFoundError: HTTP 404, or GraphQL "Could not resolve".
        ForgeForbiddenError: HTTP 401 / 403.
        ForgeError: Any other failure.
    """
    message = (res.stderr or res.stdout).strip()[:500] or f"gh exited {res.returncode}"
    if res.returncode == 127:
        raise ForgeUnavailableError(message, binary="gh")
    status_match = _HTTP_STATUS_RE.search(message)
    status = status_match.group(1) if status_match else ""
    if status == "404" or "Could not resolve to a" in message:
        raise ForgeNotFoundError(message, number=number)
    if status in {"401", "403"}:
        raise ForgeForbiddenError(message)
    raise ForgeError(message)


@dataclass(frozen=True)
class ArchiveFetch:
    """Outcome of an archive-comment fetch (SP-SAFE-MERGE-ARCHIVE-RETRY).

    Distinguishes the three cases that
    :meth:`GitHubForge._fetch_archive_comment` historically collapsed into a
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


class GitHubForge:
    """The ``gh``-backed forge: today's GitHub wrapper behind the protocol."""

    name = "github"

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
        self._protection_cache: dict[str, BranchProtection] = {}
        self.merge_status_retry_s = 2.0

    def available(self) -> bool:
        """True if a ``gh`` binary is reachable."""
        return self._gh is not None and Path(self._gh).is_file()

    @property
    def bot_username(self) -> str:
        """The GitHub login trust-bearing markers must be authored by."""
        return self._bot_login

    def _is_trusted_author(self, comment: object) -> bool:
        """True when a raw comment payload was authored by :attr:`bot_username`.

        Fails closed: an empty/unresolvable :attr:`bot_username` never
        matches, and a missing/null author never matches — a missing
        identity must never widen trust (SP-EVIDENCE-SENTINEL-AUTHOR).
        """
        return bool(self._bot_login) and comment_author_login(comment) == self._bot_login

    def is_trusted_author(self, comment: Comment) -> bool:
        """True when ``comment`` was authored by :attr:`bot_username` (fail-closed)."""
        return trusted(self._bot_login, comment.author)

    def _spawn(self, argv: list[str], *, input_data: str | None = None) -> GhResult:
        """Run a subprocess in ``cwd`` and capture output as a GhResult.

        Args:
            argv: Full argv to spawn.
            input_data: Optional stdin payload (SP-REVIEW-POST-BATCH).
        """
        return spawn(argv, cwd=self._cwd, timeout_s=self._timeout, input_data=input_data)

    def _run(self, args: list[str], *, input_data: str | None = None) -> GhResult:
        """Spawn ``gh`` with the given subargs and capture output.

        Args:
            args: Subargs appended after the ``gh`` binary path.
            input_data: Optional stdin payload, forwarded to
                :meth:`_spawn` (SP-REVIEW-POST-BATCH).
        """
        if not self.available():
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

    def git(self, args: list[str]) -> CommandResult:
        """Run ``git`` in the local clone (protocol name of :meth:`_run_git`)."""
        return self._run_git(args)

    def _pr_diff(self, pr_number: int) -> GhResult:
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
            self._run_git(["fetch", "--quiet", "origin", self._head_ref_spec(pr_number)])
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

    @staticmethod
    def _head_ref_spec(pr_number: int) -> str:
        """The server-side ref holding a PR's head (``refs/pull/<N>/head``)."""
        return f"refs/pull/{pr_number}/head"

    def _pr_view(self, pr_number: int) -> dict[str, object] | None:
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

    def _pr_create(
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

    def _pr_review_comment(
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
        comments today, so we go through the REST API.  When ``file``
        and ``line`` (or ``commit_sha``) are omitted, the comment lands
        on the PR conversation tab as a regular issue comment.

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

    def _pr_review_submit(
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

    def _pr_review_submit_batch(
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
        carrying the whole reviewer's output. The payload is fed as
        JSON over stdin via ``--input -``.

        Args:
            pr_number: PR number.
            commit_sha: Commit SHA the inline comments anchor to.
            verdict: One of "APPROVE" / "REQUEST_CHANGES" / "COMMENT";
                an unrecognised value falls back to "COMMENT".
            body: Review body markdown.
            comments: One dict per inline comment, each carrying
                ``"file"``, ``"line"`` and ``"body"``.

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

    def _list_review_id_comments(self, pr_number: int, review_id: int) -> list[dict[str, object]]:
        """Return the inline comments belonging to one specific review.

        Wraps ``GET /repos/.../pulls/{pr}/reviews/{review_id}/comments``.

        Args:
            pr_number: PR number.
            review_id: The ``id`` of a review previously created by
                :meth:`_pr_review_submit_batch`.

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

    def _paginated(self, endpoint: str, *, number: int | None) -> list[object]:
        """GET a paginated array endpoint; raise :class:`ForgeError` on failure."""
        res = self._run(["api", "--paginate", "--slurp", endpoint])
        if not res.ok:
            raise_for_gh(res, number=number)
        try:
            pages = json.loads(res.stdout) if res.stdout.strip() else []
            return flatten_paginated_pages(pages)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ForgeError(f"JSON decode failed: {str(exc)[:160]}") from exc

    def _list_review_comments(self, pr_number: int) -> list[dict[str, object]]:
        """Return every inline review comment on a PR (root + replies).

        Wraps ``GET /repos/.../pulls/{pr}/comments`` with paging.

        Returns:
            JSON-decoded list, or ``[]`` on any failure (paired with a
            structured log so the caller can fail closed without
            blowing up).
        """
        try:
            data = self._paginated(
                f"repos/:owner/:repo/pulls/{pr_number}/comments", number=pr_number
            )
        except ForgeError as exc:
            _log.warning(
                "gh_client.review_comments_decode_failed",
                pr_number=pr_number,
                error=exc.message[:200],
            )
            return []
        return [c for c in data if isinstance(c, dict)]

    def _reply_to_review_comment(
        self,
        pr_number: int,
        *,
        comment_id: int,
        body: str,
    ) -> GhResult:
        """Post a threaded reply on an existing inline review comment.

        Wraps ``POST /repos/.../pulls/{pr}/comments/{id}/replies``.

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

    def _pr_head_sha(self, pr_number: int) -> str | None:
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

    def _find_archive_comment(self, pr_number: int) -> int | None:
        """Return the id of the existing archive comment on a PR, if any.

        Walks the PR's issue-comments and matches on the marker
        :data:`~repoach.forge.protocol.ARCHIVE_MARKER`.  Returns ``None`` if no comment carries
        the marker, no marker-bearing comment was authored by
        :attr:`bot_username` (SP-EVIDENCE-SENTINEL-AUTHOR — a forged
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
            if not isinstance(c, dict) or ARCHIVE_MARKER not in (c.get("body") or ""):
                continue
            if not self._is_trusted_author(c):
                continue
            cid = c.get("id")
            if isinstance(cid, int):
                return cid
        return None

    def _find_marked_raw(
        self, pr_number: int, marker: str
    ) -> tuple[dict[str, object] | None, str | None]:
        """Return ``(trusted comment payload carrying marker, api_error)`` for a PR.

        The first issue comment that carries ``marker``, was authored by
        :attr:`bot_username` and has an integer id wins; ``api_error`` is set
        when the listing itself failed or did not decode.
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
            return None, res.stderr.strip()[:200] or f"gh exited {res.returncode}"
        try:
            comments = flatten_paginated_pages(json.loads(res.stdout))
        except (json.JSONDecodeError, ValueError) as exc:
            return None, f"JSON decode failed: {str(exc)[:160]}"
        for c in comments:
            if not isinstance(c, dict) or marker not in str(c.get("body") or ""):
                continue
            if self._is_trusted_author(c) and _int_or_none(c.get("id")) is not None:
                return c, None
        return None, None

    def _upsert_archive_comment(self, pr_number: int, *, body: str) -> GhResult:
        """Create or update the sticky archive comment on a PR.

        The comment is identified by :data:`~repoach.forge.protocol.ARCHIVE_MARKER`.  When
        found, we PATCH it; otherwise we POST a new issue comment.

        Args:
            pr_number: PR number.
            body: Markdown body — the marker is prepended when missing.

        Returns:
            :class:`GhResult`.
        """
        if ARCHIVE_MARKER not in body:
            body = f"{ARCHIVE_MARKER}\n{body}"
        return self._post_or_patch(pr_number, body, self._find_archive_comment(pr_number))

    def _post_or_patch(self, pr_number: int, body: str, existing: int | None) -> GhResult:
        """POST a new issue comment, or PATCH ``existing`` when given."""
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

    def _fetch_archive_comment(self, pr_number: int) -> str | None:
        """Return the raw body of the archive comment on a PR (or ``None``).

        Collapses API failure and genuine absence into ``None`` — use
        :meth:`_fetch_archive_comment_with_status` when the caller must
        tell the two apart.
        """
        return self._fetch_archive_comment_with_status(pr_number).body

    def _fetch_archive_comment_with_status(self, pr_number: int) -> ArchiveFetch:
        """Fetch the archive comment, distinguishing absence from API failure.

        Sibling of :meth:`_fetch_archive_comment`
        (SP-SAFE-MERGE-ARCHIVE-RETRY): the legacy method silently
        returned ``None`` when the ``gh api`` call itself failed, which
        downstream consumers misread as "no archive comment on the PR".

        Args:
            pr_number: PR number.

        Returns:
            :class:`ArchiveFetch` — ``body`` set when a comment carrying
            :data:`~repoach.forge.protocol.ARCHIVE_MARKER` **and** authored by :attr:`bot_username`
            was found, ``api_error`` set when the fetch failed, both
            ``None`` when no trusted archive comment exists.
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
            if ARCHIVE_MARKER in body and self._is_trusted_author(comment):
                return ArchiveFetch(body=body, api_error=None)
        return ArchiveFetch(body=None, api_error=None)

    def _list_open_prs(self) -> list[dict[str, object]]:
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

    def _merged_pr_merge_shas(self, base: str) -> set[str]:
        """Return every merge-commit SHA GitHub reports for PRs merged into *base*.

        Wraps ``gh pr list --state merged --base <base> --json
        mergeCommit`` — read-only. ``gh pr list`` exposes the merge
        commit as a nested ``mergeCommit`` object (``{"oid": ...}``).
        For a squash-merge that ``mergeCommit.oid`` is exactly the
        resulting commit SHA landed on *base*
        (SP-RELEASE-PROVENANCE-GH-FALLBACK).

        Args:
            base: The base branch merged PRs are listed against.

        Returns:
            The set of non-empty ``mergeCommit.oid`` values; empty when
            no PR has merged into *base* yet, the API call failed, or
            the response could not be decoded.
        """
        try:
            return self.merged_shas(base)
        except ForgeError as exc:
            _log.warning(
                "gh_client.merged_pr_merge_shas_failed",
                base=base,
                stderr=exc.message[:200],
            )
            return set()

    def pull_request(self, number: int) -> PullRequest:
        """Read one PR, normalised.

        Raises:
            ForgeError: The ``gh pr view`` call failed or did not decode.
        """
        res = self._run(
            [
                "pr",
                "view",
                str(number),
                "--json",
                "number,title,body,headRefName,baseRefName,author,state,url,headRefOid,isDraft",
            ]
        )
        if not res.ok:
            raise_for_gh(res, number=number)
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh pr view returned non-JSON: {str(exc)[:160]}") from exc
        if not isinstance(data, dict):
            raise ForgeError("gh pr view returned a non-object payload")
        return pull_request_from_github(data, number=number)

    def pr_state(self, number: int) -> PullRequestState:
        """Read only the PR's state (``gh pr view --json state``)."""
        res = self._run(["pr", "view", str(number), "--json", "state"])
        if not res.ok:
            raise_for_gh(res, number=number)
        try:
            data = json.loads(res.stdout)
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh pr view returned non-JSON: {str(exc)[:160]}") from exc
        raw = str(data.get("state") or "").upper() if isinstance(data, dict) else ""
        return _GH_STATE_TO_PROTOCOL.get(raw, "unknown")

    def head_sha(self, number: int) -> str:
        """Read only the PR's head SHA.

        Raises:
            ForgeError: The call failed or reported no head.
        """
        sha = self._pr_head_sha(number)
        if not sha:
            raise ForgeError(f"no head SHA reported for PR #{number}")
        return sha

    def diff(self, number: int) -> Diff:
        """Read the PR diff (``gh pr diff``, local-git fallback past 300 files)."""
        res = self._pr_diff(number)
        if not res.ok:
            raise_for_gh(res, number=number)
        return Diff(text=res.stdout, files=diff_files(res.stdout))

    def comments(self, number: int) -> list[Comment]:
        """Read every inline review comment (``pulls/<N>/comments``)."""
        data = self._paginated(f"repos/:owner/:repo/pulls/{number}/comments", number=number)
        return [comment_from_github(c) for c in data if isinstance(c, dict)]

    def conversation_comments(self, number: int) -> list[Comment]:
        """Read the PR's issue comments (``issues/<N>/comments``)."""
        data = self._paginated(f"repos/:owner/:repo/issues/{number}/comments", number=number)
        return [comment_from_github(c) for c in data if isinstance(c, dict)]

    def reviews(self, number: int) -> list[Review]:
        """Read the PR's reviews (``pulls/<N>/reviews``)."""
        data = self._paginated(f"repos/:owner/:repo/pulls/{number}/reviews", number=number)
        return [
            Review(
                id=_int_or_none(r.get("id")) or 0,
                author=comment_author_login(r) or "",
                state=str(r.get("state") or ""),
                body=str(r.get("body") or ""),
            )
            for r in data
            if isinstance(r, dict)
        ]

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
        """Post an issue comment, or an inline review comment when fully anchored."""
        res = self._pr_review_comment(
            number, body=body, commit_sha=commit_sha, file=path, line=line
        )
        if not res.ok:
            raise_for_gh(res, number=number)
        anchored = path is not None and line is not None and commit_sha is not None
        return Comment(
            id=_json_id(res.stdout) or 0,
            author=self._bot_login,
            body=body,
            path=path if anchored else None,
            line=line if anchored else None,
        )

    @write_barrier
    def post_review(self, number: int, review: ReviewDraft) -> Review:
        """Post a review: batched with its inline comments, or a bare verdict.

        With ``commit_sha`` set the verdict and every inline comment go
        out as one ``POST pulls/<N>/reviews`` (SP-REVIEW-POST-BATCH) and
        the created comments' ids are read back from that review. With
        no ``commit_sha`` the verdict alone is submitted through
        ``gh pr review``.

        Raises:
            ValueError: Inline comments were given without ``commit_sha``.
            ForgeError: The forge rejected the submission.
        """
        if review.commit_sha is None:
            if review.comments:
                raise ValueError("inline comments need a commit_sha to anchor to")
            res = self._pr_review_submit(number, verdict=review.verdict, summary=review.body)
            if not res.ok:
                raise_for_gh(res, number=number)
            return Review(author=self._bot_login, state=review.verdict.upper(), body=review.body)
        payload = [{"file": c.path, "line": c.line, "body": c.body} for c in review.comments]
        res = self._pr_review_submit_batch(
            number,
            commit_sha=review.commit_sha,
            verdict=review.verdict,
            body=review.body,
            comments=payload,
        )
        if not res.ok:
            raise_for_gh(res, number=number)
        review_id = _json_id(res.stdout)
        posted: list[Comment] = []
        if review_id is None:
            _log.debug("review_team.batch_review_id_parse_failed", pr_number=number)
        elif payload:
            for entry in self._list_review_id_comments(number, review_id):
                if isinstance(entry, dict):
                    posted.append(comment_from_github(entry))
        return Review(
            id=review_id or 0,
            author=self._bot_login,
            state=review.verdict.upper(),
            body=review.body,
            comments=posted,
        )

    @write_barrier
    def reply(self, number: int, comment_id: int, body: str) -> Comment:
        """Reply in the inline thread rooted at ``comment_id``."""
        res = self._reply_to_review_comment(number, comment_id=comment_id, body=body)
        if not res.ok:
            raise_for_gh(res, number=number)
        return Comment(
            id=_json_id(res.stdout) or 0,
            author=self._bot_login,
            body=body,
            in_reply_to=comment_id,
        )

    def find_marked_comment(self, number: int, marker: str) -> Comment | None:
        """Return the trusted issue comment carrying ``marker``.

        Raises:
            ForgeError: The listing failed — distinct from absence.
        """
        found, error = self._find_marked_raw(number, marker)
        if error is not None:
            raise ForgeError(error)
        return comment_from_github(found) if found is not None else None

    @write_barrier
    def upsert_marked_comment(self, number: int, marker: str, body: str) -> Comment:
        """Create or update the trusted issue comment carrying ``marker``.

        Unlike the historical archive upsert, a failed lookup raises
        instead of posting a second marker comment.

        Raises:
            ForgeError: The lookup or the write failed.
        """
        final_body = body if marker in body else f"{marker}\n{body}"
        found, error = self._find_marked_raw(number, marker)
        if error is not None:
            raise ForgeError(error)
        existing = _int_or_none(found.get("id")) if found is not None else None
        res = self._post_or_patch(number, final_body, existing)
        if not res.ok:
            raise_for_gh(res, number=number)
        return Comment(
            id=_json_id(res.stdout) or existing or 0, author=self._bot_login, body=final_body
        )

    def _fetch_status_rollup(self, pr_number: int) -> tuple[list[dict], str]:
        """Return ``(rollup_entries, error_message)`` from ``gh pr view``.

        On parse errors or non-zero exit, ``rollup_entries`` is empty and
        ``error_message`` carries a short diagnostic.
        """
        res = self._run(["pr", "view", str(pr_number), "--json", "statusCheckRollup"])
        if not res.ok:
            return [], f"gh pr view failed: {res.stderr[:200]}"
        try:
            data = json.loads(res.stdout) or {}
        except json.JSONDecodeError:
            return [], f"unparseable gh output: {res.stdout[:200]}"
        rollup = data.get("statusCheckRollup")
        if not isinstance(rollup, list):
            return [], "statusCheckRollup missing from gh output"
        return [dict(entry) for entry in rollup], ""

    def _fetch_check_runs(self, head_sha: str) -> tuple[list[dict], str]:
        """Return ``(check_run_entries, error_message)`` for a commit.

        Queries the commit's complete check-run record instead of the PR
        ``statusCheckRollup``: the rollup is blind to ``workflow_dispatch``
        runs and was observed pinning a stale CANCELLED entry over a newer
        green run at the same SHA (PR #3, 2026-07-03).  Entries are
        normalised to the rollup shape (``name`` / ``status`` /
        ``conclusion`` / ``startedAt``).
        """
        res = self._run(
            [
                "api",
                "repos/{owner}/{repo}/commits/" + head_sha + "/check-runs",
                "--paginate",
                "--jq",
                ".check_runs[] | {name, status, conclusion, startedAt: .started_at}",
            ]
        )
        if not res.ok:
            return [], f"gh api check-runs failed: {res.stderr[:200]}"
        entries: list[dict] = []
        for line in res.stdout.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            try:
                entries.append(dict(json.loads(stripped)))
            except json.JSONDecodeError:
                return [], f"unparseable check-runs line: {stripped[:200]}"
        return entries, ""

    def _required_check_names(self) -> list[str]:
        """The required checks of the integration branch, else the configured list (G5)."""
        protection = self.branch_protection(get_settings().integration_branch)
        if protection.readable and protection.required_checks:
            return list(protection.required_checks)
        return list(DEFAULT_REQUIRED_CHECK_NAMES)

    def ci_status(
        self,
        number: int,
        *,
        head_sha: str | None = None,
        required: list[str] | tuple[str, ...] | None = None,
    ) -> CheckStatus:
        """Judge the required checks at the PR head (one poll).

        Facts come from the head commit's check-runs (``head_sha`` when
        given, else resolved now so a mid-poll push is judged at the
        fresh head); the PR ``statusCheckRollup`` is only a fallback when
        the commit API fails. ``required`` ``None`` reads the branch
        protection and falls back to :data:`DEFAULT_REQUIRED_CHECK_NAMES`.

        Raises:
            ForgeError: Neither source answered.
        """
        names = list(required) if required is not None else self._required_check_names()
        sha = head_sha or self._pr_head_sha(number)
        if sha:
            rollup, err = self._fetch_check_runs(sha)
            if err and not rollup:
                _log.warning(
                    "auto_merge.check_runs_fallback_to_rollup",
                    pr_number=number,
                    head_sha=sha[:12],
                    error=err[:200],
                )
                rollup, err = self._fetch_status_rollup(number)
        else:
            rollup, err = self._fetch_status_rollup(number)
        if err and not rollup:
            raise ForgeError(err)
        failed, pending, missing = classify_required_checks(rollup, names)
        return CheckStatus(
            state=check_state(failed, pending, missing),
            checks=[
                CheckRun(
                    name=str(entry.get("name") or ""),
                    status=str(entry.get("status") or ""),
                    conclusion=str(entry.get("conclusion") or ""),
                    started_at=str(entry.get("startedAt") or ""),
                )
                for entry in rollup
            ],
            failed=failed,
            pending=pending,
            missing=missing,
            required=names,
        )

    def checks(self, number: int) -> list[CheckRun]:
        """Read every check ``gh pr checks`` reports (exit 8 still carries rows).

        Raises:
            ForgeError: The call failed or did not decode.
        """
        res = self._run(["pr", "checks", str(number), "--json", "name,state,bucket,link,workflow"])
        if not res.ok and res.returncode != 8:
            raise_for_gh(res, number=number)
        try:
            rows = json.loads(res.stdout) or []
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh pr checks returned non-JSON: {str(exc)[:160]}") from exc
        if not isinstance(rows, list):
            raise ForgeError("gh pr checks returned a non-array payload")
        return [
            CheckRun(
                name=str(r.get("name", "")),
                status=str(r.get("state", "")),
                bucket=str(r.get("bucket", "")).lower(),
                link=str(r.get("link", "")),
                workflow=str(r.get("workflow", "")),
            )
            for r in rows
            if isinstance(r, dict)
        ]

    def check_log(self, link: str) -> str | None:
        """Read ``gh run view <run> --log-failed [--job <job>]`` for a check link.

        Returns:
            The log text, or ``None`` when ``link`` names no run.

        Raises:
            ForgeError: ``gh run view`` failed.
        """
        m = _RUN_LINK_RE.search(link)
        if m is None:
            return None
        argv = ["run", "view", m.group(1), "--log-failed"]
        if m.group(2):
            argv += ["--job", m.group(2)]
        res = self._run(argv)
        if not res.ok:
            raise ForgeError(res.stderr[:200])
        return res.stdout

    def _mergeability(self, number: int) -> dict[str, object]:
        """Read the PR's merge facts; re-read once when GitHub still says ``UNKNOWN``.

        GitHub computes ``mergeStateStatus`` lazily: the first read after a
        push can be ``UNKNOWN``. One re-read after
        ``merge_status_retry_s`` seconds; a second ``UNKNOWN`` stays
        ``UNKNOWN`` and the merge is refused.
        """
        argv = ["pr", "view", str(number), "--json", MERGEABILITY_FIELDS]
        for attempt in range(2):
            res = self._run(argv)
            if not res.ok:
                raise_for_gh(res, number=number)
            try:
                view = json.loads(res.stdout)
            except json.JSONDecodeError as exc:
                raise ForgeError(f"gh pr view returned non-JSON: {str(exc)[:160]}") from exc
            if not isinstance(view, dict):
                raise ForgeError("gh pr view returned a non-object payload")
            if str(view.get("mergeStateStatus") or "").upper() != "UNKNOWN" or attempt == 1:
                return view
            time.sleep(self.merge_status_retry_s)
        return view

    @staticmethod
    def _merge_refusal(
        view: dict[str, object], head_sha: str | None
    ) -> tuple[MergeRefusal, str] | None:
        """Name why GitHub would refuse to merge now, from ``gh pr view`` facts.

        Order: state, a head that moved off ``head_sha``, draft, conflict,
        then ``mergeStateStatus`` through :data:`MERGE_STATE_STATUS` —
        ``BLOCKED`` / ``BEHIND`` / ``UNSTABLE`` count as ``pending`` while
        a check in ``statusCheckRollup`` is still running. ``None`` when
        GitHub reports the PR mergeable.
        """
        state = _GH_STATE_TO_PROTOCOL.get(str(view.get("state") or "").upper(), "unknown")
        if state != "open":
            return "not_open", f"pull request state is {view.get('state')!r}"
        current = str(view.get("headRefOid") or "")
        if head_sha and current != head_sha:
            return "head_moved", f"head moved: expected {head_sha[:12]}, now {current[:12]}"
        merge_state = str(view.get("mergeStateStatus") or "").upper()
        if view.get("isDraft") or merge_state == "DRAFT":
            return "draft", "pull request is a draft"
        if merge_state == "DIRTY" or str(view.get("mergeable") or "").upper() == "CONFLICTING":
            return "conflict", f"mergeStateStatus={merge_state}"
        if merge_state not in MERGE_STATE_STATUS:
            return "unrecognized", f"mergeStateStatus={merge_state!r} is not a documented value"
        rollup = view.get("statusCheckRollup")
        if merge_state in _WAITS_ON_CHECKS and _rollup_pending(rollup):
            return "pending", f"mergeStateStatus={merge_state} with checks still running"
        predicted = MERGE_STATE_STATUS[merge_state]
        if predicted is not None:
            return predicted, f"mergeStateStatus={merge_state}"
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
        """Merge after reading GitHub's merge facts, pinned to the head.

        :meth:`_mergeability` is read first and :meth:`_merge_refusal`
        decides; only then ``gh pr merge --<method> [--match-head-commit
        <sha>] [--delete-branch]`` runs, so GitHub itself also refuses a
        head that moved in between.

        Raises:
            ForgeMergeRefusedError: GitHub would refuse or refused; its
                ``reason`` comes from the read facts, else ``refused``.
        """
        refusal = self._merge_refusal(self._mergeability(number), head_sha)
        if refusal is not None:
            raise ForgeMergeRefusedError(refusal[1], reason=refusal[0])
        args = ["pr", "merge", str(number), _MERGE_METHOD_FLAGS[MergeMethod(method)]]
        if head_sha:
            args += ["--match-head-commit", head_sha]
        if delete_branch:
            args.append("--delete-branch")
        res = self._run(args)
        if not res.ok:
            raise ForgeMergeRefusedError((res.stderr or res.stdout)[-500:])
        m = _MERGE_SHA_RE.search(res.stdout)
        return MergeResult(merged=True, sha=m.group(1) if m else None, message=res.stdout[-500:])

    def branch_protection(self, branch: str) -> BranchProtection:
        """Read ``branches/<branch>/protection``; unreadable (403/404) is a value."""
        cached = self._protection_cache.get(branch)
        if cached is not None:
            return cached
        res = self._run(["api", f"repos/{{owner}}/{{repo}}/branches/{branch}/protection"])
        protection = BranchProtection(readable=False)
        if res.ok:
            try:
                data = json.loads(res.stdout)
            except json.JSONDecodeError as exc:
                _log.warning("forge.github.protection_decode_failed", error=str(exc)[:200])
                data = None
            if isinstance(data, dict):
                protection = _protection_from_github(data)
        else:
            _log.info(
                "forge.github.protection_unreadable",
                branch=branch,
                stderr=(res.stderr or res.stdout).strip()[:200],
            )
        self._protection_cache[branch] = protection
        return protection

    def open_pull_requests(self, base: str | None = None) -> list[PullRequest]:
        """List open PRs, optionally only those targeting ``base``."""
        args = [
            "pr",
            "list",
            "--state",
            "open",
            "--json",
            "number,title,headRefName,baseRefName,author,url,headRefOid,isDraft",
            "--limit",
            "100",
        ]
        if base:
            args += ["--base", base]
        res = self._run(args)
        if not res.ok:
            raise_for_gh(res)
        try:
            data = json.loads(res.stdout) or []
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh pr list returned non-JSON: {str(exc)[:160]}") from exc
        return [
            pull_request_from_github(entry, number=0) for entry in data if isinstance(entry, dict)
        ]

    def merged_shas(self, base: str) -> set[str]:
        """Return the ``mergeCommit.oid`` of every PR merged into ``base``.

        Raises:
            ForgeError: The listing failed or did not decode.
        """
        res = self._run(
            [
                "pr",
                "list",
                "--state",
                "merged",
                "--base",
                base,
                "--json",
                "mergeCommit",
                "--limit",
                "500",
            ]
        )
        if not res.ok:
            raise_for_gh(res)
        try:
            data = json.loads(res.stdout) if res.stdout.strip() else []
        except json.JSONDecodeError as exc:
            raise ForgeError(f"gh pr list returned non-JSON: {str(exc)[:160]}") from exc
        if not isinstance(data, list):
            return set()
        merge_shas: set[str] = set()
        for entry in data:
            if not isinstance(entry, dict):
                continue
            merge_commit = entry.get("mergeCommit")
            if not isinstance(merge_commit, dict):
                continue
            sha = merge_commit.get("oid")
            if isinstance(sha, str) and sha:
                merge_shas.add(sha)
        return merge_shas

    def create_pull_request(
        self, *, head: str, base: str, title: str, body: str, draft: bool = False
    ) -> PullRequest:
        """Open a PR with ``gh pr create``; the URL is the last ``http`` line printed."""
        res = self._pr_create(head=head, base=base, title=title, body=body, draft=draft)
        if not res.ok:
            raise_for_gh(res)
        url = res.stdout.strip()
        for line in reversed(res.stdout.strip().splitlines()):
            if line.startswith("http"):
                url = line.strip()
                break
        m = _PULL_URL_RE.search(url)
        return PullRequest(
            number=int(m.group(1)) if m else 0,
            title=title,
            body=body,
            author=self._bot_login,
            head_ref=head,
            base_ref=base,
            url=url,
            draft=draft,
        )

    def fetch_head(self, number: int) -> str:
        """Fetch ``refs/pull/<N>/head`` into the local object store.

        Raises:
            ForgeError: ``git fetch`` failed.
        """
        ref = self._head_ref_spec(number)
        res = self._run_git(["fetch", "--quiet", "origin", ref])
        if not res.ok:
            raise ForgeError(res.stderr.strip()[:200] or f"git fetch {ref} failed")
        return ref


def _protection_from_github(data: dict[str, object]) -> BranchProtection:
    """Normalise a ``branches/<b>/protection`` payload."""
    checks = data.get("required_status_checks")
    names: list[str] = []
    if isinstance(checks, dict):
        for entry in checks.get("checks") or []:
            if isinstance(entry, dict) and isinstance(entry.get("context"), str):
                names.append(entry["context"])
        for context in checks.get("contexts") or []:
            if isinstance(context, str) and context not in names:
                names.append(context)
    force = data.get("allow_force_pushes")
    allow_force = bool(force.get("enabled")) if isinstance(force, dict) else False
    return BranchProtection(required_checks=names, allow_force_push=allow_force, readable=True)


GhCli = GitHubForge
"""Historical name of :class:`GitHubForge`, kept for one release."""

__all__ = [
    "DEFAULT_REQUIRED_CHECK_NAMES",
    "ArchiveFetch",
    "GhCli",
    "GhResult",
    "GitHubForge",
    "check_state",
    "classify_required_checks",
    "comment_author_login",
    "flatten_paginated_pages",
    "raise_for_gh",
]
