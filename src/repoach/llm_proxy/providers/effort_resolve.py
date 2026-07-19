"""Per-cell reasoning-effort resolution (SP-CHAINPILOT-EFFORT-RESOLVE, 2a-3-iii).

Phase 2a-3-iii of the Chain Autopilot arc. The effort sweep (2a-3-ii) accumulates
a ``cell_effort_probe`` time-series — how each ``(provider, model)`` cell behaved
when asked to reason at its single-pass effort. This leaf reads that series and
decides, per cell, which effort (if any) is safe to wire: the conservative,
evidence-based map the generic transport (2a-3-iv) applies.

Effort is worth wiring on a cell only where it both took effect and stayed safe:
a probe is *handled* when it emitted reasoning **and** a visible answer survived
(``reasoning_chars > 0 and content_chars > 0``). A cell that reasoned but starved
its answer, or never reasoned at all, resolves to ``None`` — leave effort off,
the 0b-3 safe default. Aggregation (min-sample + handled-fraction) honors
Principle 6 (one outcome never decides); the full rolling-window perf model stays
Phase 2d/3. The resolver is pure over rows; a thin loader fetches them.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from repoach.llm_proxy.providers.effort_probe_store import (
    EffortProbeRow,
    fetch_effort_probes,
)

__all__ = ["EffortResolution", "load_cell_efforts", "resolve_cell_efforts"]


@dataclass(frozen=True, slots=True)
class EffortResolution:
    """The resolved effort decision for one ``(provider, model)`` cell.

    Attributes:
        provider_id: The cell's provider.
        model_id: The cell's model.
        effort: The effort to wire for this cell, or ``None`` to leave effort off
            (the safe default when no effort qualified).
        handled: The count of ``thinking_handled`` probes backing the decision
            (for the chosen effort; ``0`` when none qualified).
        samples: The probes considered for the chosen effort (``0`` when none
            qualified).
    """

    provider_id: str
    model_id: str
    effort: str | None
    handled: int
    samples: int


def _is_handled(row: EffortProbeRow) -> bool:
    """True when the probe reasoned and still returned a visible answer."""
    return row.reasoning_chars > 0 and row.content_chars > 0


def resolve_cell_efforts(
    rows: Sequence[EffortProbeRow],
    *,
    min_samples: int = 1,
    handled_fraction: float = 0.5,
) -> dict[tuple[str, str], EffortResolution]:
    """Resolve the effort to wire for every cell present in *rows*. Pure, total.

    Groups *rows* by ``(provider_id, model_id)`` and, within a cell, by
    ``effort_used`` (``None`` efforts carry nothing to wire). A candidate effort
    qualifies when its samples reach *min_samples* and its handled fraction
    reaches *handled_fraction*; the cell resolves to the qualifying effort with
    the most handled probes (ties broken toward the lexicographically larger
    effort). A cell with no qualifying effort resolves to ``effort=None`` but is
    still present in the map.

    Args:
        rows: The ``cell_effort_probe`` rows to judge (the caller scopes recency
            and volume via the store's filters).
        min_samples: The minimum probes an effort needs before it can resolve.
        handled_fraction: The minimum ``handled / samples`` for an effort to
            qualify.

    Returns:
        A map ``(provider_id, model_id) -> EffortResolution``; every cell in
        *rows* appears exactly once.
    """
    by_cell: dict[tuple[str, str], list[EffortProbeRow]] = defaultdict(list)
    for row in rows:
        by_cell[(row.provider_id, row.model_id)].append(row)

    resolved: dict[tuple[str, str], EffortResolution] = {}
    for (provider_id, model_id), cell_rows in by_cell.items():
        by_effort: dict[str, list[EffortProbeRow]] = defaultdict(list)
        for row in cell_rows:
            if row.effort_used is not None:
                by_effort[row.effort_used].append(row)

        best: tuple[int, str] | None = None
        for effort, effort_rows in by_effort.items():
            samples = len(effort_rows)
            handled = sum(1 for row in effort_rows if _is_handled(row))
            if samples >= min_samples and handled >= handled_fraction * samples:
                candidate = (handled, effort)
                if best is None or candidate > best:
                    best = candidate

        if best is None:
            resolved[(provider_id, model_id)] = EffortResolution(provider_id, model_id, None, 0, 0)
        else:
            handled, effort = best
            resolved[(provider_id, model_id)] = EffortResolution(
                provider_id, model_id, effort, handled, len(by_effort[effort])
            )
    return resolved


def load_cell_efforts(
    db_path: Path,
    *,
    since: datetime | None = None,
    provider_id: str | None = None,
    model_id: str | None = None,
    min_samples: int = 1,
    handled_fraction: float = 0.5,
) -> dict[tuple[str, str], EffortResolution]:
    """Fetch ``cell_effort_probe`` rows then resolve them.

    Thin convenience over :func:`fetch_effort_probes` + :func:`resolve_cell_efforts`.

    Args:
        db_path: SQLite path of the effort-probe store.
        since: When set, only rows with ``recorded_at >= since``.
        provider_id: When set, only rows for that provider.
        model_id: When set, only rows for that model.
        min_samples: Forwarded to :func:`resolve_cell_efforts`.
        handled_fraction: Forwarded to :func:`resolve_cell_efforts`.

    Returns:
        The resolved per-cell effort map.
    """
    rows = fetch_effort_probes(db_path, since=since, provider_id=provider_id, model_id=model_id)
    return resolve_cell_efforts(rows, min_samples=min_samples, handled_fraction=handled_fraction)
