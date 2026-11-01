"""Owner commands: a maintainer settles a finding no agent can judge (SP-OWNER-SETTLES-A-FINDING).

Some blocking findings state facts outside the repository: a forge
setting, a runner's kernel parameter, a protected environment. The
refuter judges from the cited file's window and can never see them, so
the gate would keep blocking at every head. A maintainer of the
repository settles such a finding with a reason, in a comment:

- ``/resolve <reason>`` or ``/reopen`` as a reply in the finding's own
  inline thread (the thread anchors the finding by its file and line);
- ``/resolve #<finding-id> <reason>`` or ``/reopen #<finding-id>`` in any
  comment, for a finding that was never posted.

``review pr`` applies the commands before the verdict is derived:
:func:`apply_owner_commands` reads both comment lists of the pull
request, asks the forge whether each author is a maintainer
(:meth:`~repoach.forge.protocol.Forge.is_maintainer`; never the comment
text, never the bot), moves the finding to ``owner_resolved`` (or back to
the status it held before, on ``/reopen``) and records the command in
``owner_commands`` under its comment id, so a re-run applies it and
answers it once. The bot's own command is recorded and answered by
nothing, and a command in an edited comment is refused before any
permission is read, answered once per login. A command it ignores (no
reason, not a maintainer,
no such finding, nothing to change) gets one reply saying why; a
non-maintainer is answered once per login, whatever the number of their
commands. A reason echoed in a reply is a code span, so its mentions and
Markdown stay inert. The replies are posted only by a posting run; a dry
run returns them as lines.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from ..core.logging import get_logger
from ..forge.protocol import Comment, Forge, ForgeError, is_bot_login
from .findings import (
    OWNER_SETTLEABLE_STATUSES,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    update_finding_status,
)
from .owner_store import (
    OWNER_REOPEN,
    OWNER_RESOLVE,
    OwnerCommandRow,
    fetch_owner_commands,
    mark_reply_posted,
    record_owner_command,
    revise_owner_command,
)

_log = get_logger(__name__)

REASON_MIN_CHARS = 20
"""A ``/resolve`` reason shorter than this is refused (constraint ``reason_min_chars``)."""

REASON_MAX_CHARS = 1000
"""A longer reason is cut here before it is stored (constraint ``reason_max_chars``)."""

_COMMAND_RE = re.compile(r"^/(?P<verb>resolve|reopen)(?=\s|$)(?P<rest>.*)\Z", re.DOTALL)
_FINDING_ID_RE = re.compile(r"^#(?P<id>\d+)(?=\s|$)")


class OwnerVerb(StrEnum):
    """The two owner commands."""

    RESOLVE = "resolve"
    REOPEN = "reopen"


class OwnerOutcome(StrEnum):
    """What a handled command did."""

    APPLIED = "applied"
    NOT_OWNER = "not_owner"
    NO_REASON = "no_reason"
    UNKNOWN_FINDING = "unknown_finding"
    AMBIGUOUS = "ambiguous"
    NOOP = "noop"
    BOT = "bot"
    EDITED = "edited"


@dataclass(frozen=True)
class OwnerCommand:
    """One parsed command.

    Attributes:
        verb: ``resolve`` or ``reopen``.
        finding_id: The ``#<id>`` the command names, ``None`` when it names none.
        reason: The reason, whitespace collapsed (empty when none was given).
    """

    verb: OwnerVerb
    finding_id: int | None
    reason: str


@dataclass(frozen=True)
class OwnerSettlement:
    """One finding an owner settled, as the report prints it.

    Attributes:
        finding_id: The ledger id.
        login: The maintainer who settled it.
        reason: The recorded reason.
        comment_id: The forge id of the ``/resolve`` comment.
        head_sha: The head it was settled at.
    """

    finding_id: int
    login: str
    reason: str
    comment_id: int
    head_sha: str

    def line(self) -> str:
        """The report line: ``owner-resolved #<id> by <login>: <reason>``."""
        return f"owner-resolved #{self.finding_id} by {self.login}: {self.reason}"

    def as_json(self) -> dict[str, object]:
        """The entry of ``owner_resolved`` in the ``review pr`` JSON."""
        return {
            "finding_id": self.finding_id,
            "login": self.login,
            "reason": self.reason,
            "comment_id": self.comment_id,
            "head_sha": self.head_sha,
        }


@dataclass
class OwnerCommandsRun:
    """What one :func:`apply_owner_commands` call did.

    Attributes:
        handled: The commands handled for the first time by this run.
        settled: Every finding of the pull request currently owner-resolved.
        unposted: The replies this run did not post (a dry run, or a
            failed post), each kept pending for the next posting run.
        unreadable: Logins whose permission could not be read; their
            commands were left unhandled, to be retried.
        error: Why the comments could not be read, empty when they were.
    """

    handled: list[OwnerCommandRow] = field(default_factory=list)
    settled: list[OwnerSettlement] = field(default_factory=list)
    unposted: list[str] = field(default_factory=list)
    unreadable: list[str] = field(default_factory=list)
    error: str = ""

    def settlement_lines(self) -> list[str]:
        """One ``owner-resolved #<id> by <login>: <reason>`` line per settled finding."""
        return [settlement.line() for settlement in self.settled]

    def lines(self) -> list[str]:
        """The stderr lines of ``review pr``: settlements, then what was not done."""
        lines = self.settlement_lines()
        if self.handled:
            applied = sum(1 for row in self.handled if row.outcome == OwnerOutcome.APPLIED)
            lines.append(
                f"owner commands: {applied} applied, {len(self.handled) - applied} ignored"
            )
        lines.extend(f"owner reply not posted: {reply}" for reply in self.unposted)
        lines.extend(
            f"owner commands: permission of {login} unreadable, its commands left for the next run"
            for login in self.unreadable
        )
        if self.error:
            lines.append(f"owner commands: not read: {self.error}")
        return lines

    def as_json(self) -> list[dict[str, object]]:
        """The ``owner_resolved`` array of the ``review pr`` JSON."""
        return [settlement.as_json() for settlement in self.settled]

    def handled_json(self) -> list[dict[str, object]]:
        """The ``owner_commands`` array of the ``review pr`` JSON."""
        return [
            {
                "comment_id": row.comment_id,
                "command": row.command,
                "author": row.author,
                "finding_id": row.finding_id,
                "outcome": row.outcome,
            }
            for row in self.handled
        ]


def parse_command(body: str) -> OwnerCommand | None:
    """Parse an owner command from a comment body.

    The body, stripped, must start with ``/resolve`` or ``/reopen`` followed
    by whitespace or its end; an optional ``#<finding-id>`` comes next, and
    the rest of the body, every non-printable character (an ANSI escape)
    turned to a space, whitespace collapsed and cut at
    :data:`REASON_MAX_CHARS`, is the reason.

    Args:
        body: The comment's Markdown body.

    Returns:
        The command, or ``None`` when the body is not one.
    """
    match = _COMMAND_RE.match(body.strip())
    if match is None:
        return None
    rest = match.group("rest").strip()
    finding_id: int | None = None
    named = _FINDING_ID_RE.match(rest)
    if named is not None:
        finding_id = int(named.group("id"))
        rest = rest[named.end() :]
    printable = "".join(ch if ch.isprintable() else " " for ch in rest)
    reason = " ".join(printable.split())[:REASON_MAX_CHARS]
    return OwnerCommand(verb=OwnerVerb(match.group("verb")), finding_id=finding_id, reason=reason)


def inline_code(text: str) -> str:
    """Render ``text`` as one Markdown code span no backtick inside can close.

    A reason is a person's text echoed in a comment the bot authors: as a
    code span, its ``@mentions``, links and Markdown stay inert.

    Args:
        text: The text to show verbatim.

    Returns:
        The code span, its delimiter one backtick longer than the longest
        backtick run in ``text``.
    """
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    return f"{fence} {text} {fence}"


def _anchored(findings: Sequence[Finding], root: Comment) -> list[Finding]:
    """The findings an inline thread's root anchors: same file, line in the cited range."""
    if root.path is None or root.line is None:
        return []
    return [
        finding
        for finding in findings
        if finding.file == root.path
        and finding.line_start <= root.line <= max(finding.line_end, finding.line_start)
    ]


@dataclass(frozen=True)
class _Decision:
    """The outcome, target and answer computed for one command."""

    outcome: OwnerOutcome
    finding: Finding | None
    reply: str


def _settleable(finding: Finding) -> bool:
    """Whether ``/resolve`` may settle the finding: blocking and still unsettled."""
    return finding.severity is Severity.BLOCKING and finding.status in OWNER_SETTLEABLE_STATUSES


def _target(
    command: OwnerCommand,
    *,
    findings: dict[int, Finding],
    root: Comment | None,
) -> _Decision | Finding:
    """The finding the command addresses, or the refusal when it addresses none or several."""
    if command.finding_id is not None:
        named = findings.get(command.finding_id)
        if named is None:
            return _Decision(
                OwnerOutcome.UNKNOWN_FINDING,
                None,
                f"Ignored: this pull request has no finding #{command.finding_id}.",
            )
        return named
    usage = f"`/{command.verb.value} #<finding-id>{' <reason>' if command.verb is OwnerVerb.RESOLVE else ''}`"
    if root is None:
        return _Decision(
            OwnerOutcome.UNKNOWN_FINDING,
            None,
            f"Ignored: a comment outside a finding's thread must name the finding: {usage}.",
        )
    anchored = _anchored(list(findings.values()), root)
    if command.verb is OwnerVerb.RESOLVE:
        live = [finding for finding in anchored if _settleable(finding)]
    else:
        live = [f for f in anchored if f.status is FindingStatus.OWNER_RESOLVED]
    if len(live) == 1:
        return live[0]
    if len(live) > 1:
        ids = ", ".join(f"#{finding.id}" for finding in live)
        return _Decision(
            OwnerOutcome.AMBIGUOUS,
            None,
            f"Ignored: this thread anchors findings {ids}; name one: {usage}.",
        )
    if anchored:
        return anchored[0]
    return _Decision(
        OwnerOutcome.UNKNOWN_FINDING,
        None,
        f"Ignored: this thread anchors no finding of this pull request; name one: {usage}.",
    )


def _decide(
    command: OwnerCommand,
    *,
    login: str,
    head_sha: str,
    findings: dict[int, Finding],
    root: Comment | None,
    rows: Sequence[OwnerCommandRow],
) -> _Decision:
    """Decide what an owner's command does, and the one answer it gets."""
    if command.verb is OwnerVerb.RESOLVE and len(command.reason) < REASON_MIN_CHARS:
        return _Decision(
            OwnerOutcome.NO_REASON,
            None,
            f"Ignored: `/resolve` needs a reason of at least {REASON_MIN_CHARS} characters, "
            "recorded with the settlement: `/resolve <reason>` in the finding's thread, or "
            "`/resolve #<finding-id> <reason>` on the pull request.",
        )
    target = _target(command, findings=findings, root=root)
    if isinstance(target, _Decision):
        return target
    if command.verb is OwnerVerb.RESOLVE:
        if not _settleable(target):
            return _Decision(
                OwnerOutcome.NOOP,
                target,
                f"No change: finding #{target.id} is {target.severity.value} and "
                f"{target.status.value}, not an open blocking finding.",
            )
        return _Decision(
            OwnerOutcome.APPLIED,
            target,
            f"Finding #{target.id} owner-resolved by @{login} at {head_sha[:7]}: "
            f"{inline_code(command.reason)}. The gate no longer counts it; `/reopen` counts "
            "it again.",
        )
    if target.status is not FindingStatus.OWNER_RESOLVED:
        return _Decision(
            OwnerOutcome.NOOP,
            target,
            f"No change: finding #{target.id} is {target.status.value}, not owner-resolved.",
        )
    return _Decision(
        OwnerOutcome.APPLIED,
        target,
        f"Finding #{target.id} reopened by @{login}: the gate counts it again."
        + (
            ""
            if _last_resolve(rows, target.id or 0)
            else " No owner settlement of it was recorded, so it is restored as verified."
        ),
    )


def _last_resolve(rows: Sequence[OwnerCommandRow], finding_id: int) -> OwnerCommandRow | None:
    """The latest applied ``/resolve`` of ``finding_id``."""
    applied = [
        row for row in rows if row.finding_id == finding_id and row.resolution == OWNER_RESOLVE
    ]
    return applied[-1] if applied else None


def _settlement_fields(finding: Finding, command: OwnerCommand) -> dict[str, str]:
    """The ``resolution`` and ``previous_*`` fields of an applied command's row.

    Read from the finding before it moves, so the row can be written first.
    """
    return {
        "resolution": OWNER_RESOLVE if command.verb is OwnerVerb.RESOLVE else OWNER_REOPEN,
        "previous_status": finding.status.value,
        "previous_method": finding.verification_method,
        "previous_result": finding.verification_result,
        "previous_checked_at": finding.checked_at_sha,
    }


def _move(
    db_path: Path,
    decision: _Decision,
    command: OwnerCommand,
    *,
    login: str,
    head_sha: str,
    rows: Sequence[OwnerCommandRow],
) -> bool:
    """Move the finding of an applied command; False when the ledger refused the transition."""
    finding = decision.finding
    if finding is None or finding.id is None:
        return False
    if command.verb is OwnerVerb.RESOLVE:
        return update_finding_status(
            db_path,
            finding.id,
            FindingStatus.OWNER_RESOLVED,
            verification_method=OWNER_RESOLVE,
            verification_result=f"owner-resolved by {login}: {command.reason}",
            checked_at_sha=head_sha,
        )
    resolved = _last_resolve(rows, finding.id)
    if resolved is None:
        _log.warning(
            "owner_commands.reopen_without_recorded_resolve",
            finding_id=finding.id,
            consequence="restored as verified with an empty verification; it blocks again",
        )
    return update_finding_status(
        db_path,
        finding.id,
        FindingStatus(resolved.previous_status) if resolved else FindingStatus.VERIFIED,
        verification_method=resolved.previous_method if resolved else "",
        verification_result=resolved.previous_result if resolved else "",
        checked_at_sha=resolved.previous_checked_at if resolved else "",
    )


def _commands(forge: Forge, pr_number: int) -> list[tuple[Comment, Comment | None]]:
    """Every comment of the pull request with the inline thread root it sits in.

    Inline comments carry their root (a root is its own); conversation
    comments carry ``None``. Ordered by creation time, then id, so a
    ``/resolve`` followed by a ``/reopen`` applies in that order.
    """
    inline = forge.comments(pr_number)
    roots = {comment.id: comment for comment in inline if comment.in_reply_to is None}
    pairs: list[tuple[Comment, Comment | None]] = [
        (comment, roots.get(comment.in_reply_to) if comment.in_reply_to else comment)
        for comment in inline
    ]
    pairs.extend((comment, None) for comment in forge.conversation_comments(pr_number))
    return sorted(pairs, key=lambda pair: (pair[0].created_at, pair[0].id))


def _findings_by_id(db_path: Path, pr_number: int) -> dict[int, Finding]:
    """The pull request's findings by ledger id, read once and again after each applied move."""
    return {f.id: f for f in fetch_findings(db_path, pr_number) if f.id is not None}


def _maintainer(forge: Forge, login: str, cache: dict[str, bool]) -> bool:
    """Whether ``login`` is a maintainer, read once per run per login.

    Raises:
        ForgeError: The forge could not say.
    """
    if login not in cache:
        cache[login] = forge.is_maintainer(login)
    return cache[login]


def _author_refusal(
    forge: Forge,
    comment: Comment,
    *,
    cache: dict[str, bool],
    rows: Sequence[OwnerCommandRow],
) -> tuple[OwnerOutcome, str] | None:
    """Why the comment's author or form stops the command, with its answer; ``None`` lets it on.

    The bot's own command is answered by nothing; an edited comment is
    refused, since anyone with write access may edit someone else's
    comment on GitHub; a non-maintainer is refused. Both refusals are
    answered once per login and outcome, so no commenter can make the bot
    post one reply per comment.

    Raises:
        ForgeError: The author's permission could not be read.
    """
    login = comment.author
    if not login or is_bot_login(forge.bot_username, login):
        return OwnerOutcome.BOT, ""
    if comment.edited:
        outcome = OwnerOutcome.EDITED
        reply = (
            "Ignored: an edited comment is not read as a command; post the command in a "
            "new comment."
        )
    elif _maintainer(forge, login, cache):
        return None
    else:
        outcome = OwnerOutcome.NOT_OWNER
        reply = (
            f"Ignored: @{login} is not a maintainer of this repository; only a maintainer's "
            "`/resolve` or `/reopen` settles a finding."
        )
    answered = any(row.author == login and row.outcome == outcome for row in rows)
    return outcome, "" if answered else reply


def _handle(
    forge: Forge,
    db_path: Path,
    comment: Comment,
    root: Comment | None,
    command: OwnerCommand,
    *,
    pr_number: int,
    head_sha: str,
    findings: dict[int, Finding],
    cache: dict[str, bool],
    rows: list[OwnerCommandRow],
) -> tuple[OwnerCommandRow, _Decision | None]:
    """Decide and describe one command not handled before; nothing is written here.

    Returns:
        The row to record and, for a command to apply, its decision (the
        row then already names the resolution and what the finding held).

    Raises:
        ForgeError: The author's permission could not be read.
    """
    login = comment.author
    refusal = _author_refusal(forge, comment, cache=cache, rows=rows)
    if refusal is not None:
        outcome, reply = refusal
        finding_id = command.finding_id
    else:
        decision = _decide(
            command, login=login, head_sha=head_sha, findings=findings, root=root, rows=rows
        )
        outcome, reply = decision.outcome, decision.reply
        finding_id = decision.finding.id if decision.finding else command.finding_id
    row = OwnerCommandRow(
        pr_number=pr_number,
        comment_id=comment.id,
        command=command.verb.value,
        author=login,
        finding_id=finding_id,
        reason=command.reason,
        head_sha=head_sha,
        outcome=outcome,
        reply=reply,
        reply_to=root.id if root is not None else None,
    )
    if refusal is not None or decision.outcome is not OwnerOutcome.APPLIED or not decision.finding:
        return row, None
    return row.model_copy(update=_settlement_fields(decision.finding, command)), decision


def _post_replies(
    forge: Forge,
    db_path: Path,
    rows: Sequence[OwnerCommandRow],
    *,
    pr_number: int,
    post: bool,
) -> list[str]:
    """Post every pending answer once; return those left unposted."""
    unposted: list[str] = []
    for row in rows:
        if not row.reply or row.reply_posted:
            continue
        if not post:
            unposted.append(row.reply)
            continue
        try:
            if row.reply_to is not None:
                forge.reply(pr_number, row.reply_to, row.reply)
            else:
                forge.post_comment(pr_number, row.reply)
        except ForgeError as exc:
            _log.warning(
                "owner_commands.reply_failed",
                pr_number=pr_number,
                comment_id=row.comment_id,
                error=exc.message[:200],
                consequence="the reply stays pending for the next posting run",
            )
            unposted.append(row.reply)
            continue
        mark_reply_posted(db_path, pr_number=pr_number, comment_id=row.comment_id)
    return unposted


def owner_settlements(db_path: Path, pr_number: int) -> list[OwnerSettlement]:
    """Every finding of the pull request currently owner-resolved, with who and why.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.

    Returns:
        One settlement per ``owner_resolved`` finding, by finding id.
    """
    rows = fetch_owner_commands(db_path, pr_number)
    settled: list[OwnerSettlement] = []
    for finding in fetch_findings(db_path, pr_number, status=FindingStatus.OWNER_RESOLVED):
        if finding.id is None:
            continue
        resolved = _last_resolve(rows, finding.id)
        if resolved is None:
            continue
        settled.append(
            OwnerSettlement(
                finding_id=finding.id,
                login=resolved.author,
                reason=resolved.reason,
                comment_id=resolved.comment_id,
                head_sha=resolved.head_sha,
            )
        )
    return settled


def apply_owner_commands(
    forge: Forge,
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    post: bool,
) -> OwnerCommandsRun:
    """Apply every owner command of the pull request not applied before.

    Args:
        forge: The forge the pull request lives on.
        db_path: The ledger database.
        pr_number: The pull / merge request.
        head_sha: The reviewed head, recorded with each command.
        post: Whether the answers may be posted (False on a dry run).

    Returns:
        What the run handled, the current settlements and the answers it
        did not post. Unreadable comments apply nothing (``error``); a
        login whose permission is unreadable has its commands left
        unhandled, to be retried by the next run.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: The ledger could not be read or
            written; the orchestrator logs it and applies nothing. No
            :class:`~repoach.forge.protocol.ForgeError` is raised: a forge
            failure is reported in the returned run (``error``,
            ``unreadable``, ``unposted``).
    """
    init_findings_schema(db_path)
    run = OwnerCommandsRun()
    try:
        pairs = _commands(forge, pr_number)
    except ForgeError as exc:
        _log.warning("owner_commands.comments_unreadable", pr_number=pr_number, error=exc.message)
        run.error = exc.message[:200]
        run.settled = owner_settlements(db_path, pr_number)
        return run
    rows = fetch_owner_commands(db_path, pr_number)
    seen = {row.comment_id for row in rows}
    cache: dict[str, bool] = {}
    findings = _findings_by_id(db_path, pr_number)
    for comment, root in pairs:
        command = parse_command(comment.body)
        if command is None or comment.id in seen:
            continue
        try:
            row, decision = _handle(
                forge,
                db_path,
                comment,
                root,
                command,
                pr_number=pr_number,
                head_sha=head_sha,
                findings=findings,
                cache=cache,
                rows=rows,
            )
        except ForgeError as exc:
            _log.warning(
                "owner_commands.permission_unreadable",
                pr_number=pr_number,
                login=comment.author,
                error=exc.message[:200],
            )
            if comment.author not in run.unreadable:
                run.unreadable.append(comment.author)
            continue
        seen.add(comment.id)
        if not record_owner_command(db_path, row):
            continue
        if decision is not None:
            moved = _move(
                db_path, decision, command, login=comment.author, head_sha=head_sha, rows=rows
            )
            if moved:
                findings = _findings_by_id(db_path, pr_number)
            else:
                row = row.model_copy(
                    update={
                        "outcome": OwnerOutcome.NOOP,
                        "resolution": "",
                        "reply": f"No change: finding #{row.finding_id} could not be moved; "
                        "its status changed during this run.",
                    }
                )
                revise_owner_command(db_path, row)
        rows.append(row)
        run.handled.append(row)
    run.unposted = _post_replies(
        forge, db_path, fetch_owner_commands(db_path, pr_number), pr_number=pr_number, post=post
    )
    run.settled = owner_settlements(db_path, pr_number)
    return run
