"""Model-first chains — live gather + regenerate entrypoint (SP-MFC-REGEN).

Slice 5 of the model-first chains arc (``docs/model_first_chains_architecture.md``).
The runnable entrypoint: gather the live inputs — the Artificial Analysis ranking
(slice 1), the ``(provider × model)`` matrix, the equivalence table, and the
per-cell probe latency — then regenerate ``chains.env`` (slice 4) behind the apply
flag, shadow by default.

It runs **alongside** the existing Chain Autopilot rather than repointing its
armed loop: this slice adds the model-first regeneration path; retiring the
chainpilot's mechanical edit cycle is a separate, operator-gated follow-up taken
after the model-first output is validated live.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from pathlib import Path

import httpx

from ferova.core.logging import get_logger
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.aa_ingest import fetch_aa_ranking
from ferova.llm_proxy.providers.benchmark_equivalences import load_equivalence_table
from ferova.llm_proxy.providers.cell_probe_store import CellProbeRow, fetch_cell_probes
from ferova.llm_proxy.providers.model_matrix import sweep_model_matrix
from ferova.llm_proxy.routing.chain_generate import GenerateResult, regenerate

_log = get_logger(__name__)

__all__ = [
    "gather_and_regenerate",
    "speed_for_from_rows",
]


def speed_for_from_rows(
    rows: Sequence[CellProbeRow],
) -> Callable[[str, str], float | None]:
    """Reduce newest-first probe rows to a per-cell latest-latency lookup.

    The rows arrive newest-first (``fetch_cell_probes``), so the first row seen
    for a ``(provider_id, model_id)`` carries its latest latency.

    Args:
        rows: Probe rows, newest-first.

    Returns:
        ``(provider_id, model_id) -> latest latency_s | None`` (``None`` for an
        unseen cell).
    """
    latest: dict[tuple[str, str], float | None] = {}
    for row in rows:
        key = (row.provider_id, row.model_id)
        if key not in latest:
            latest[key] = row.latency_s

    def speed_for(provider_id: str, model_id: str) -> float | None:
        return latest.get((provider_id, model_id))

    return speed_for


async def gather_and_regenerate(
    settings: Settings,
    *,
    client: httpx.AsyncClient,
    chains_path: Path,
    db_path: Path,
    enabled: bool,
) -> GenerateResult:
    """Gather live inputs and regenerate ``chains.env`` (shadow unless enabled).

    Args:
        settings: Supplies the AA key and provider credentials.
        client: A caller-owned ``httpx.AsyncClient`` for the matrix sweep.
        chains_path: The ``chains.env`` to regenerate.
        db_path: The probe SQLite database.
        enabled: The apply gate; ``False`` computes + logs the diff without
            writing.

    Returns:
        The slice-4 :class:`GenerateResult`.

    Raises:
        AaIngestError: The AA key is missing or the fetch fails.
        GenerateError: A tier slot is absent from ``chains.env``.
        SelectError: An anchor model is absent from the ranking.
    """
    ranking = fetch_aa_ranking(settings)
    matrix = await sweep_model_matrix(settings, client)
    equivalences = load_equivalence_table()
    speed_for = speed_for_from_rows(fetch_cell_probes(db_path))
    current = chains_path.read_text(encoding="utf-8")
    result = regenerate(
        current,
        ranking,
        matrix,
        equivalences,
        speed_for=speed_for,
        chains_path=chains_path,
        enabled=enabled,
    )
    _log.info(
        "mfc_regenerate",
        cells=len(matrix.cells),
        changed=result.changed,
        written=result.written,
        opus=len(result.chains.get("opus", ())),
        sonnet=len(result.chains.get("sonnet", ())),
        haiku=len(result.chains.get("haiku", ())),
    )
    return result
