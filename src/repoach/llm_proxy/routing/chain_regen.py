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

Before reading the probe rows, a bounded in-cycle cell-health sweep
(SP-REGEN-FRESH-CELLS) refreshes only the cells this cycle is about to read:
the cells already referenced by ``chains.env`` plus the live ranking's
candidate cells, capped per provider with chain-ref cells prioritized, with
open_router cells dropped when the credits snapshot reports a below-floor
balance. Probing runs per-provider under a concurrency + pacing bound; a
rate-limited (``http=429``) outcome is retried once and, if still
rate-limited, logged and never persisted as cell health.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from repoach.core.logging import get_logger
from repoach.health.credits import get_cached_credits
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.aa_ingest import AaRanking, fetch_aa_ranking, normalize_model_name
from repoach.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    load_equivalence_table,
)
from repoach.llm_proxy.providers.cell_probe_store import (
    CellProbeRow,
    fetch_cell_probes,
    record_cell_probes,
)
from repoach.llm_proxy.providers.cell_probe_sweep import CellHealth, sweep_cell_health
from repoach.llm_proxy.providers.model_matrix import (
    ModelCell,
    ProviderModelMatrix,
    sweep_model_matrix,
)
from repoach.llm_proxy.routing.chain_generate import TIER_SLOT, GenerateResult, regenerate

_log = get_logger(__name__)

_RATE_LIMITED_DETAIL = "http=429"

__all__ = [
    "StaleCellsError",
    "gather_and_regenerate",
    "speed_for_from_rows",
]


class StaleCellsError(Exception):
    """Raised when the freshness-windowed cell-probe read finds no fresh rows.

    Signals the G5 refusal (SP-REGEN-FRESH-CELLS): the bounded in-cycle sweep
    ran, but the ``since``-windowed read of the cell-probe store came back
    empty, or its newest row is older than ``regen_max_cell_age_h`` — the exact
    2026-07-10 incident condition, caught here before a regeneration
    conclusion is drawn from a stale table. The ``regenerate-chains`` CLI
    command is expected to catch this, print a one-line reason, and exit
    non-zero rather than let it propagate as an unhandled traceback.
    """


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


def _parse_chain_ref_cells(current_content: str) -> tuple[ModelCell, ...]:
    """Extract sweepable ``(provider, model)`` cells from the current chain refs.

    Reads every ``MODEL_*`` slot present in *current_content*; the
    ``claude_code`` tail of a chain has no ``/v1/models`` and is never a sweep
    target, so it is skipped. A missing slot or a malformed entry contributes
    no cells rather than raising — chain-ref extraction here is best-effort
    ahead of the sweep, not the source of truth for the file's structure.

    Args:
        current_content: The current ``chains.env`` text.

    Returns:
        The referenced cells, de-duplicated, first-occurrence order.
    """
    cells: list[ModelCell] = []
    seen: set[tuple[str, str]] = set()
    lines = current_content.splitlines()
    for slot in TIER_SLOT.values():
        prefix = f"{slot}="
        for line in lines:
            if not line.startswith(prefix):
                continue
            for part in line[len(prefix) :].split(","):
                stripped = part.strip()
                if not stripped or "/" not in stripped:
                    continue
                provider_id, _, model_id = stripped.partition("/")
                if provider_id == "claude_code" or not model_id:
                    continue
                key = (provider_id, model_id)
                if key in seen:
                    continue
                seen.add(key)
                cells.append(ModelCell(provider_id, model_id))
    return tuple(cells)


def _candidate_cells_from_ranking(
    ranking: AaRanking,
    matrix: ProviderModelMatrix,
    equivalences: EquivalenceTable,
) -> tuple[ModelCell, ...]:
    """The matrix cells serving any ranked model, in matrix order.

    Joins the ranking to the matrix through the same benchmark-name
    equivalence resolution the regeneration itself uses: each matrix cell's
    aliases are normalized and checked against the ranking's (already
    normalized) model names.

    Args:
        ranking: The collapsed capability ranking.
        matrix: The live ``(provider × model)`` matrix.
        equivalences: The benchmark name↔id resolver.

    Returns:
        The candidate cells, de-duplicated, matrix order.
    """
    wanted = {model.name for model in ranking.models}
    cells: list[ModelCell] = []
    seen: set[tuple[str, str]] = set()
    for cell in matrix.cells:
        aliases = equivalences.aliases_for_model_id(cell.model_id)
        if not any(normalize_model_name(alias) in wanted for alias in aliases):
            continue
        key = (cell.provider_id, cell.model_id)
        if key in seen:
            continue
        seen.add(key)
        cells.append(cell)
    return tuple(cells)


def _bounded_cell_set(
    chain_ref_cells: Sequence[ModelCell],
    candidate_cells: Sequence[ModelCell],
    *,
    per_provider_cap: int,
) -> tuple[ModelCell, ...]:
    """Cap the sweep to at most *per_provider_cap* cells per provider.

    Chain-ref cells take priority within each provider's cap; candidate cells
    fill any remaining room. ``per_provider_cap <= 0`` disables the sweep for
    the run (the operational escape hatch — a later freshness read then
    refuses loudly on its own once it finds no fresh rows).

    Args:
        chain_ref_cells: Cells already referenced by ``chains.env``.
        candidate_cells: Cells derived from the live ranking.
        per_provider_cap: The maximum number of cells kept per provider.

    Returns:
        The bounded cell set, de-duplicated, chain refs first per provider.
    """
    if per_provider_cap <= 0:
        return ()
    per_provider_count: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    bounded: list[ModelCell] = []
    for cell in (*chain_ref_cells, *candidate_cells):
        key = (cell.provider_id, cell.model_id)
        if key in seen:
            continue
        count = per_provider_count.get(cell.provider_id, 0)
        if count >= per_provider_cap:
            continue
        seen.add(key)
        per_provider_count[cell.provider_id] = count + 1
        bounded.append(cell)
    return tuple(bounded)


async def _drop_paid_cells_if_low_credits(
    cells: Sequence[ModelCell],
    settings: Settings,
    client: httpx.AsyncClient,
) -> tuple[tuple[ModelCell, ...], int]:
    """Drop ``open_router`` cells when the credits snapshot is below the floor.

    A ``None`` snapshot (unavailable) never triggers a skip: unknown is not
    the same as exhausted, so the cells are kept and the caller's planned-sweep
    log carries the true (zero) skip count.

    Args:
        cells: The bounded cell set.
        settings: Supplies the OpenRouter credential and the credits floor.
        client: A caller-owned ``httpx.AsyncClient`` for the credits fetch.

    Returns:
        The filtered cells and the number of ``open_router`` cells dropped.
    """
    if not any(cell.provider_id == "open_router" for cell in cells):
        return tuple(cells), 0
    snapshot = await get_cached_credits(
        settings.open_router_api_key,
        client=client,
        ttl_s=settings.credits_health_cache_ttl_s,
    )
    if snapshot is None or snapshot.remaining >= settings.credits_floor_usd:
        return tuple(cells), 0
    kept = tuple(cell for cell in cells if cell.provider_id != "open_router")
    return kept, len(cells) - len(kept)


async def _probe_bounded_cells(
    cells: Sequence[ModelCell],
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    max_concurrency: int,
    pacing_s: float,
    retry_backoff_s: float,
) -> list[CellHealth]:
    """Probe *cells* per-provider, retrying a lone 429 once before dropping it.

    A rate-limited probe is observer interference, not cell death: a
    ``detail == "http=429"`` outcome is retried once after *retry_backoff_s*;
    a probe still 429 after the retry is logged (``cell_probe_rate_limited``)
    and excluded from the returned results, so it is never persisted as cell
    health.

    Args:
        cells: The bounded, credits-filtered cell set.
        settings: Proxy settings for endpoint + credential resolution.
        client: A caller-owned ``httpx.AsyncClient``.
        max_concurrency: Forwarded to :func:`sweep_cell_health` per provider.
        pacing_s: Forwarded to :func:`sweep_cell_health` per provider.
        retry_backoff_s: Delay before the single 429 retry.

    Returns:
        The non-429 probe results, in per-provider sweep order.
    """
    if not cells:
        return []
    results: list[CellHealth] = []
    providers = dict.fromkeys(cell.provider_id for cell in cells)
    for provider_id in providers:
        sub = ProviderModelMatrix(
            cells=tuple(cell for cell in cells if cell.provider_id == provider_id),
            listings=(),
        )
        healths = await sweep_cell_health(
            sub,
            settings,
            client,
            max_concurrency=max_concurrency,
            pacing_s=pacing_s,
        )
        for health in healths:
            if health.detail != _RATE_LIMITED_DETAIL:
                results.append(health)
                continue
            if retry_backoff_s > 0:
                await asyncio.sleep(retry_backoff_s)
            retry_matrix = ProviderModelMatrix(
                cells=(ModelCell(health.provider_id, health.model_id),), listings=()
            )
            retried = await sweep_cell_health(
                retry_matrix,
                settings,
                client,
                max_concurrency=max_concurrency,
                pacing_s=0.0,
            )
            retried_health = retried[0] if retried else health
            if retried_health.detail == _RATE_LIMITED_DETAIL:
                _log.warning(
                    "cell_probe_rate_limited",
                    provider=retried_health.provider_id,
                    model=retried_health.model_id,
                )
                continue
            results.append(retried_health)
    return results


async def _sweep_and_persist_bounded_cells(
    settings: Settings,
    client: httpx.AsyncClient,
    matrix: ProviderModelMatrix,
    equivalences: EquivalenceTable,
    ranking: AaRanking,
    current_content: str,
    db_path: Path,
) -> None:
    """Bound, probe, and persist a fresh in-cycle cell-health sweep.

    Chain-ref cells (already in ``chains.env``) plus candidate cells (from the
    live ranking) are capped per-provider with chain refs prioritized,
    ``open_router`` cells are dropped when the credits snapshot reports a
    below-floor remaining balance, and the survivors are probed per-provider
    under a concurrency + pacing bound. A 429 outcome is retried once and, if
    still rate-limited, logged and dropped rather than persisted.

    Args:
        settings: Supplies the sweep caps, pacing, and provider credentials.
        client: A caller-owned ``httpx.AsyncClient``.
        matrix: The live ``(provider × model)`` matrix.
        equivalences: The benchmark name↔id resolver.
        ranking: The collapsed capability ranking.
        current_content: The current ``chains.env`` text.
        db_path: The probe SQLite database.
    """
    chain_ref_cells = _parse_chain_ref_cells(current_content)
    candidate_cells = _candidate_cells_from_ranking(ranking, matrix, equivalences)
    bounded = _bounded_cell_set(
        chain_ref_cells,
        candidate_cells,
        per_provider_cap=settings.regen_sweep_per_provider_cap,
    )
    bounded, skipped_paid = await _drop_paid_cells_if_low_credits(bounded, settings, client)
    _log.info(
        "regen_sweep_planned",
        cells=len(bounded),
        per_provider=settings.regen_sweep_per_provider_cap,
        skipped_paid=skipped_paid,
    )
    healths = await _probe_bounded_cells(
        bounded,
        settings,
        client,
        max_concurrency=settings.regen_sweep_per_provider_concurrency,
        pacing_s=settings.regen_sweep_pacing_s,
        retry_backoff_s=settings.regen_sweep_retry_backoff_s,
    )
    if healths:
        record_cell_probes(db_path, healths, recorded_at=datetime.now(UTC))


async def gather_and_regenerate(
    settings: Settings,
    *,
    client: httpx.AsyncClient,
    chains_path: Path,
    db_path: Path,
    enabled: bool,
    ranking: AaRanking | None = None,
) -> GenerateResult:
    """Gather live inputs and regenerate ``chains.env`` (shadow unless enabled).

    Runs a bounded in-cycle cell-health sweep (chain-ref cells + ranking
    candidates, per-provider capped, credits-gated, 429-filtered) before
    reading the probe rows, so the regeneration reasons on freshly refreshed
    cells rather than whatever the table last happened to hold.

    Args:
        settings: Supplies the AA key, provider credentials, and sweep knobs.
        client: A caller-owned ``httpx.AsyncClient`` for the matrix sweep, the
            bounded cell-health sweep, and the credits check.
        chains_path: The ``chains.env`` to regenerate.
        db_path: The probe SQLite database.
        enabled: The apply gate; ``False`` computes + logs the diff without
            writing.
        ranking: A pre-built ranking; when ``None`` (the default) it is
            fetched via :func:`fetch_aa_ranking`. Tests inject a pre-built
            ranking to stay offline.

    Returns:
        The slice-4 :class:`GenerateResult`.

    Raises:
        AaIngestError: The AA key is missing or the fetch fails.
        GenerateError: A tier slot is absent from ``chains.env``.
        SelectError: An anchor model is absent from the ranking.
    """
    if ranking is None:
        ranking = fetch_aa_ranking(settings)
    matrix = await sweep_model_matrix(settings, client)
    equivalences = load_equivalence_table()
    current = chains_path.read_text(encoding="utf-8")
    try:
        await _sweep_and_persist_bounded_cells(
            settings, client, matrix, equivalences, ranking, current, db_path
        )
    except Exception as exc:
        _log.warning("chain_regen_sweep_failed", error=str(exc))
        raise StaleCellsError(f"in-cycle cell sweep failed: {exc}") from exc
    now = datetime.now(UTC)
    max_age = timedelta(hours=settings.regen_max_cell_age_h)
    since = now - max_age
    rows = fetch_cell_probes(db_path, since=since)
    newest = max((row.recorded_at for row in rows), default=None)
    if newest is None or newest < since:
        _log.warning(
            "chain_regen_stale_cells",
            newest=newest.isoformat() if newest is not None else None,
            max_age_h=settings.regen_max_cell_age_h,
        )
        raise StaleCellsError(
            f"no cell-probe rows fresher than {settings.regen_max_cell_age_h}h "
            f"(newest={newest.isoformat() if newest is not None else 'none'})"
        )
    speed_for = speed_for_from_rows(rows)
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
