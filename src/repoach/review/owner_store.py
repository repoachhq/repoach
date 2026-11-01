"""The ``owner_commands`` table: one row per owner command comment, applied once.

Persistence only; parsing and applying the commands is
:mod:`repoach.review.owner_commands` (SP-OWNER-SETTLES-A-FINDING).

A row is keyed by the pull request and the forge comment id, so a
re-run of ``review pr`` at the same head or a later one never applies a
command twice and never answers it twice: ``reply_posted`` records
whether the one answer reached the forge, and a dry run leaves it
pending for the next posting run.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    create_engine,
    insert,
    select,
    update,
)
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created

_log = get_logger(__name__)

_metadata = MetaData()

OWNER_RESOLVE = "owner_resolve"
"""The ``resolution`` of an applied ``/resolve``."""

OWNER_REOPEN = "owner_reopen"
"""The ``resolution`` of an applied ``/reopen``."""

owner_commands = Table(
    "owner_commands",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False, index=True),
    Column("comment_id", Integer, nullable=False),
    Column("command", String, nullable=False),
    Column("author", String, nullable=False),
    Column("finding_id", Integer, nullable=True),
    Column("reason", Text, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("outcome", String, nullable=False),
    Column("resolution", String, nullable=False),
    Column("previous_status", String, nullable=False),
    Column("previous_method", String, nullable=False),
    Column("previous_result", Text, nullable=False),
    Column("previous_checked_at", String, nullable=False),
    Column("reply", Text, nullable=False),
    Column("reply_to", Integer, nullable=True),
    Column("reply_posted", Integer, nullable=False),
    Column("recorded_at", String, nullable=False),
    UniqueConstraint("pr_number", "comment_id", name="uq_owner_commands_comment"),
)
"""One row per owner command comment (``db:table:owner_commands``)."""


class OwnerCommandRow(BaseModel):
    """One handled owner command, as stored.

    Attributes:
        pr_number: The pull / merge request.
        comment_id: The forge id of the command comment (the replay key).
        command: ``resolve`` or ``reopen``.
        author: The comment author's forge login.
        finding_id: The finding the command named or anchored, when one did.
        reason: The recorded reason (empty when none was given).
        head_sha: The head the command was handled at.
        outcome: What was done (:class:`~repoach.review.owner_commands.OwnerOutcome`).
        resolution: ``owner_resolve`` / ``owner_reopen`` when applied, else empty.
        previous_status: The finding's status before an applied command.
        previous_method: Its ``verification_method`` before.
        previous_result: Its ``verification_result`` before.
        previous_checked_at: Its ``checked_at_sha`` before.
        reply: The one answer to the command (empty: none is due).
        reply_to: The thread root to answer in; ``None`` answers on the pull request.
        reply_posted: True once the answer reached the forge.
        recorded_at: UTC ISO-8601 time the command was handled.
    """

    pr_number: int
    comment_id: int
    command: str
    author: str
    finding_id: int | None = None
    reason: str = ""
    head_sha: str = ""
    outcome: str
    resolution: str = ""
    previous_status: str = ""
    previous_method: str = ""
    previous_result: str = ""
    previous_checked_at: str = ""
    reply: str = ""
    reply_to: int | None = None
    reply_posted: bool = False
    recorded_at: str = ""


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine on ``db_path``, creating its directory."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_owner_schema(db_path: Path) -> None:
    """Create the ``owner_commands`` table if absent (idempotent).

    Args:
        db_path: The ledger database.
    """
    ensure_schema_created(_engine_for(db_path), _metadata)


def fetch_owner_commands(db_path: Path, pr_number: int) -> list[OwnerCommandRow]:
    """Return the pull request's handled commands, in the order they were handled.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.

    Returns:
        Every stored row of ``pr_number``, oldest first.
    """
    init_owner_schema(db_path)
    stmt = (
        select(owner_commands)
        .where(owner_commands.c.pr_number == pr_number)
        .order_by(owner_commands.c.id)
    )
    with _engine_for(db_path).connect() as conn:
        rows = conn.execute(stmt).mappings().fetchall()
    return [
        OwnerCommandRow.model_validate({**row, "reply_posted": bool(row["reply_posted"])})
        for row in rows
    ]


def record_owner_command(db_path: Path, row: OwnerCommandRow) -> bool:
    """Store one handled command; refuse a comment already stored.

    Args:
        db_path: The ledger database.
        row: The handled command.

    Returns:
        True when the row was stored, False when the comment was already
        handled (a concurrent run stored it first).
    """
    init_owner_schema(db_path)
    values = row.model_dump(exclude={"recorded_at", "reply_posted"})
    values["reply_posted"] = int(row.reply_posted)
    values["recorded_at"] = row.recorded_at or datetime.now(UTC).isoformat()
    try:
        with _engine_for(db_path).begin() as conn:
            conn.execute(insert(owner_commands).values(**values))
    except IntegrityError as exc:
        _log.info(
            "owner_commands.already_recorded",
            pr_number=row.pr_number,
            comment_id=row.comment_id,
            error=str(exc)[:120],
        )
        return False
    return True


def mark_reply_posted(db_path: Path, *, pr_number: int, comment_id: int) -> None:
    """Record that the answer to ``comment_id`` reached the forge.

    Args:
        db_path: The ledger database.
        pr_number: The pull / merge request.
        comment_id: The command comment answered.
    """
    stmt = (
        update(owner_commands)
        .where(
            owner_commands.c.pr_number == pr_number,
            owner_commands.c.comment_id == comment_id,
        )
        .values(reply_posted=1)
    )
    with _engine_for(db_path).begin() as conn:
        conn.execute(stmt)
