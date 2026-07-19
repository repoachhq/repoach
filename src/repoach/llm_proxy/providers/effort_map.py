"""Resolved-effort runtime singleton (SP-CHAINPILOT-EFFORT-MAP, 2a-3-iv-a).

The request-time lookup the generic transport reads (2a-3-iv-b) to decide which
``reasoning_effort`` to request for a ``(provider, model)`` cell. It mirrors the
health-breaker singleton exactly: a process-level instance seeded once at
``AppRuntime.startup`` from the ``cell_effort_probe`` series (2a-3-iii) and
consulted on the hot path — because per-request DB reads are not affordable and
providers are built lazily, so there is no instance to inject into at startup.

The conservative default for a missing or unresolved cell is ``None`` (leave
effort off), so behavior stays the 0b-3 default until the effort sweep has
populated the table. This half ships the singleton and its seeder only —
unwired: nothing seeds it at startup and nothing reads it in production yet
(2a-3-iv-b adds the startup seed call and the transport read).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from repoach.llm_proxy.providers.effort_resolve import load_cell_efforts


class EffortMap:
    """The resolved ``(provider, model) -> reasoning_effort`` lookup.

    Stores only cells with a non-``None`` effort; a missing cell reads back as
    ``None`` (leave effort off — the safe default). Mutated in place (one
    instance, no global rebind), mirroring the breaker singleton.
    """

    def __init__(self, efforts: Mapping[tuple[str, str], str | None] | None = None) -> None:
        """Build the map, keeping only the cells with a non-``None`` effort.

        Args:
            efforts: The per-cell efforts to wire (``None`` values are dropped).
        """
        self._efforts: dict[tuple[str, str], str] = {}
        if efforts is not None:
            self.replace(efforts)

    def effort_for(self, provider_id: str, model_id: str) -> str | None:
        """Return the resolved effort for a cell, or ``None`` to leave effort off.

        Args:
            provider_id: The cell's provider id.
            model_id: The cell's model id.

        Returns:
            The resolved effort string, or ``None`` when the cell is absent or
            resolved to no effort.
        """
        return self._efforts.get((provider_id, model_id))

    def replace(self, efforts: Mapping[tuple[str, str], str | None]) -> None:
        """Swap the contents for *efforts*, dropping ``None`` values."""
        self._efforts = {cell: effort for cell, effort in efforts.items() if effort is not None}

    def clear(self) -> None:
        """Empty the map (test isolation / reset)."""
        self._efforts = {}

    def __len__(self) -> int:
        """The number of wired cells."""
        return len(self._efforts)


_EFFORT_MAP = EffortMap()


def get_effort_map() -> EffortMap:
    """Return the process-level resolved-effort singleton."""
    return _EFFORT_MAP


def reset_effort_map() -> None:
    """Clear the singleton's state — the unit suite resets per test."""
    _EFFORT_MAP.clear()


def seed_effort_map(
    *,
    db_path: Path,
    min_samples: int = 1,
    handled_fraction: float = 0.5,
) -> int:
    """Seed the singleton from the ``cell_effort_probe`` series.

    Resolves the per-cell efforts (2a-3-iii) and installs the cells whose
    resolution carries a non-``None`` effort.

    Args:
        db_path: SQLite path of the effort-probe store.
        min_samples: Forwarded to :func:`load_cell_efforts`.
        handled_fraction: Forwarded to :func:`load_cell_efforts`.

    Returns:
        The number of cells wired with a non-``None`` effort.
    """
    resolutions = load_cell_efforts(
        db_path, min_samples=min_samples, handled_fraction=handled_fraction
    )
    _EFFORT_MAP.replace({cell: res.effort for cell, res in resolutions.items()})
    return len(_EFFORT_MAP)
