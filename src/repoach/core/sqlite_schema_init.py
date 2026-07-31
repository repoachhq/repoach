"""Shared race-proof SQLite schema-creation helper.

`SP-SCHEMA-INIT-RACE-GENERALIZE`. `SP-FINDINGS-INIT-RACE` diagnosed and
fixed a real production race in `repoach.review.findings`: two SQLite
connections (same process or different processes) racing the very
first `CREATE TABLE` of a fresh database can interleave, so the
loser's `create_all(checkfirst=True)` raises `OperationalError: table
... already exists` even though SQLite DDL is transactional and never
leaves a half-built table behind. That fix — an in-process
`threading.Lock` plus a bounded, convergent retry loop — protected
exactly one call site. This leaf module generalizes it to any
SQLAlchemy `MetaData`, so every sibling store can route its own
`create_all(checkfirst=True)` call through one shared, race-proof
implementation instead of reimplementing the pattern unprotected.
"""

from __future__ import annotations

import threading

from sqlalchemy import MetaData, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError

from .logging import get_logger

logger = get_logger(__name__)

_INIT_SCHEMA_LOCK = threading.Lock()
"""Serializes concurrent first-creation within one process, across every caller.

`create_all(checkfirst=True)` builds a metadata's tables sequentially,
so two in-process threads racing the very first creation for the same
engine can interleave: the loser's `CREATE TABLE` fails against the
winner's already-committed table, and if the loser's failure is
observed before the winner has gone on to create its remaining
tables, a re-check at that instant can still report one missing.
Holding one shared lock around the whole `create_all` call for every
caller makes each in-process caller either run its own sequence to
completion before the next one starts, or find every table already
there and no-op through `checkfirst` — turning the in-process race
into deterministic serialization rather than relying on retries alone
to paper over interleavings. Cross-process races (separate SQLite
connections in separate interpreters, which no in-process lock can
reach) are still handled by the bounded retry below.
"""

_INIT_SCHEMA_MAX_ATTEMPTS = 5
"""Bound on `create_all` retries in :func:`ensure_schema_created`.

Tables are only ever added, never dropped, so each retry's
`checkfirst=True` skips whatever the previous attempt (or a racing
process) already committed and creates only what is still missing —
the sequence converges to a no-op within a handful of attempts. Five
comfortably covers every sibling schema's table count; it exists only
to keep a genuine, persistent failure (e.g. an unwritable database
file) from retrying forever.
"""


def ensure_schema_created(engine: Engine, metadata: MetaData) -> None:
    """Create every table in *metadata* against *engine*, race-proof.

    Generalizes the fix ``SP-FINDINGS-INIT-RACE`` shipped for
    ``pr_findings`` / ``pr_review_integrity`` to any SQLAlchemy
    ``MetaData``: an in-process lock serializes concurrent
    first-creation within one process, and a bounded retry loop
    absorbs the ``OperationalError`` a losing cross-process
    ``CREATE TABLE`` surfaces as, since SQLite DDL is transactional
    and never leaves a half-built table behind.

    Args:
        engine: SQLAlchemy engine bound to the target database.
        metadata: The ``MetaData`` whose declared tables must exist.

    Raises:
        OperationalError: The database remains unusable after every
            retry -- at least one declared table still absent.
    """
    with _INIT_SCHEMA_LOCK:
        _create_all_with_retries(engine, metadata)


def _create_all_with_retries(engine: Engine, metadata: MetaData) -> None:
    """Run ``create_all(checkfirst=True)`` to convergence across racing creators.

    A losing ``CREATE TABLE`` from a racing SQLite connection surfaces
    as ``OperationalError`` even though SQLite DDL is transactional
    and never leaves a half-built table behind. Retrying re-enters
    ``checkfirst=True``, which skips every table that now exists
    (whichever process created it) and creates only what is still
    missing, so at most one retry per remaining table is needed before
    all of *metadata*'s tables are present. The bounded loop re-raises
    the last ``OperationalError`` only if, after exhausting
    :data:`_INIT_SCHEMA_MAX_ATTEMPTS`, at least one declared table
    genuinely never came to exist -- the signal that this was a real
    database failure rather than a resolved creation race.

    Args:
        engine: SQLAlchemy engine bound to the target database.
        metadata: The ``MetaData`` whose declared tables must exist.

    Raises:
        OperationalError: The database remains unusable after every
            retry -- at least one declared table still absent.
    """
    last_error: OperationalError | None = None
    for attempt in range(_INIT_SCHEMA_MAX_ATTEMPTS):
        try:
            metadata.create_all(engine, checkfirst=True)
            return
        except OperationalError as exc:
            last_error = exc
            logger.info(
                "core.sqlite_schema_init.retry",
                attempt=attempt,
                error=str(exc),
            )
    inspector = inspect(engine)
    if all(inspector.has_table(table_name) for table_name in metadata.tables):
        return
    assert last_error is not None
    raise last_error
