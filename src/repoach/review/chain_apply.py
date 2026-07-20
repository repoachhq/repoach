"""SP-CHAINPILOT-APPLY-WRITE — write a planned rewrite to disk (Phase 3d-2).

The only slice that mutates the live ``chains.env``, and it does so only behind a
flag (``enabled``, OFF by default) — the rest of the arc has built and shadow-run
the whole pipeline without ever touching the authoritative file. Given a
:class:`ChainRewritePlan` from 3d-1c, this:

- journals every planned mutation + cold-start via the 3c audit log with its
  **true per-row** ``applied`` status: a row is ``applied=True`` only when this
  run wrote AND that edit actually landed (in ``plan.rewrite.applied``); an
  ``advise`` (no structural edit) or an edit the rewriter refused is always
  ``applied=False`` — so the ledger a future loop reads never claims a change
  that did not happen;
- when enabled AND the new content differs from disk, stages the new content to a
  temp file, backs the current file up to ``chains.env.bak``, preserves the
  original file mode, and swaps it in with ``os.replace`` — atomic against a
  process crash and concurrent readers, with the previous version one ``.bak``
  away. A failed write cleans up its temp, journals the attempt as not-applied
  (never silent) and re-raises, leaving the original file intact.

It adds no policy: which models move and where was decided by 3b + 3d-1c; the
mechanical safety (backstop, never-empty, …) was enforced by 3d-1a. 3e wires the
cadence and supplies ``enabled`` from settings.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from repoach.review.audit_log import record_mutations
from repoach.review.chain_plan import ChainRewritePlan, mutation_to_edit
from repoach.review.decision import PlannedMutation

_BACKUP_SUFFIX = ".bak"


@dataclass(frozen=True)
class ApplyResult:
    """The outcome of :func:`apply_chain_rewrite`.

    Attributes:
        written: Whether the new content was written to ``chains.env`` this run
            (``True`` only when enabled and the content actually changed).
        backup_path: The ``.bak`` written before the change, or ``None`` if no
            write happened.
        journaled: The number of mutation rows recorded in the audit log.
    """

    written: bool
    backup_path: Path | None
    journaled: int


def _stage_temp(path: Path, content: str) -> Path:
    """Write ``content`` to a fresh temp file beside ``path``; clean up on failure."""
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(content)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise
    return tmp


def _journal(
    db_path: Path,
    mutations: Sequence[PlannedMutation],
    cold_starts: Sequence[PlannedMutation],
    applied_edits: frozenset[object],
    *,
    written: bool,
    recorded_at: datetime,
) -> int:
    """Record each mutation with its true ``applied`` status (two batches)."""
    applied_rows: list[PlannedMutation] = []
    shadow_rows: list[PlannedMutation] = []
    for mutation in mutations:
        edit = mutation_to_edit(mutation)
        if written and edit is not None and edit in applied_edits:
            applied_rows.append(mutation)
        else:
            shadow_rows.append(mutation)
    (applied_rows if written else shadow_rows).extend(cold_starts)

    total = 0
    if applied_rows:
        total += record_mutations(
            db_path, applied_rows, applied=True, recorded_at=recorded_at, detail="chainpilot apply"
        )
    if shadow_rows:
        total += record_mutations(
            db_path,
            shadow_rows,
            applied=False,
            recorded_at=recorded_at,
            detail="chainpilot shadow",
        )
    return total


def apply_chain_rewrite(
    plan: ChainRewritePlan,
    mutations: Sequence[PlannedMutation],
    *,
    db_path: Path,
    chains_path: Path,
    recorded_at: datetime,
    enabled: bool = False,
) -> ApplyResult:
    """Journal a planned rewrite and, if enabled, write it to ``chains.env``.

    Args:
        plan: The shadow rewrite from 3d-1c (new content + cold-start mutations).
        mutations: The decision engine's mutations (3b), journalled alongside the
            cold-starts with their true per-row applied status.
        db_path: SQLite path for the 3c audit log.
        chains_path: The authoritative ``chains.env`` to (maybe) write.
        recorded_at: One UTC timestamp for this run's journal rows.
        enabled: The flag — write to disk only when ``True`` (OFF by default).

    Returns:
        The :class:`ApplyResult` (whether written, the backup path, journal count).
    """
    to_journal = [*mutations, *plan.cold_starts]
    current = chains_path.read_text(encoding="utf-8") if chains_path.exists() else None
    should_write = enabled and current is not None and plan.rewrite.new_content != current

    backup_path: Path | None = None
    written = False
    if should_write and current is not None:
        tmp = _stage_temp(chains_path, plan.rewrite.new_content)
        try:
            backup_path = chains_path.with_name(chains_path.name + _BACKUP_SUFFIX)
            backup_path.write_text(current, encoding="utf-8")
            os.chmod(tmp, chains_path.stat().st_mode & 0o777)
            os.replace(tmp, chains_path)
        except OSError:
            tmp.unlink(missing_ok=True)
            record_mutations(
                db_path,
                to_journal,
                applied=False,
                recorded_at=recorded_at,
                detail="chainpilot apply failed",
            )
            raise
        written = True

    journaled = _journal(
        db_path,
        mutations,
        plan.cold_starts,
        frozenset(plan.rewrite.applied),
        written=written,
        recorded_at=recorded_at,
    )
    return ApplyResult(
        written=written, backup_path=backup_path if written else None, journaled=journaled
    )
