"""SP-CHAINPILOT-LOOP — the autopilot cadence (Phase 3e).

The slice that finally turns the arc into a loop: one cycle sweeps the live
provider×model matrix and probes every cell, attributes faults (3a), scores the
models (2d), decides mutations (3b), plans the shadow rewrite (3d-1c) and applies
it behind the flag (3d-2). Everything it composes is already built, tested and
shadow-run; this is the wiring + the entry point a routine/cron calls.

The cycle is split so the decision half is testable without a network: the async
:func:`run_autopilot_cycle` does the I/O (sweep + record), then hands off to the
pure-ish synchronous :func:`plan_and_apply` (DB + injected matrix → decide →
plan → apply). The write stays gated by ``enabled`` (3d-2), so a routine can run
the whole loop in shadow — recording what it *would* do — until the operator arms
it from settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import httpx

from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.attribution import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_OK_FRACTION,
    attribute_faults,
    cell_is_healthy,
    summarize_cells,
)
from repoach.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from repoach.llm_proxy.providers.benchmark_prior import BenchmarkRanking
from repoach.llm_proxy.providers.cell_probe_store import (
    CellProbeRow,
    fetch_cell_probes,
    record_cell_probes,
)
from repoach.llm_proxy.providers.cell_probe_sweep import sweep_cell_health
from repoach.llm_proxy.providers.model_matrix import ProviderModelMatrix, sweep_model_matrix
from repoach.review.chain_apply import apply_chain_rewrite
from repoach.review.chain_plan import DEFAULT_MAX_MUTATIONS, plan_chain_rewrite
from repoach.review.decision import DEFAULT_SIGMA, plan_mutations
from repoach.review.perf_aggregate import aggregate_model_performance

DEFAULT_LOOKBACK = timedelta(hours=24)
"""How far back a cycle looks at cell probes — the rolling window for attribution
and healthy-cell selection, so stale observations never mask the current state
(a model that dies this cycle must not stay 'healthy' on lifetime-aggregated ok
probes). A design parameter, overridable per call."""


@dataclass(frozen=True)
class CycleReport:
    """A one-line summary of an autopilot cycle, for logging / the CLI.

    Attributes:
        matrix_cells: How many ``(provider, model)`` cells the sweep saw.
        faults: How many cell faults attribution produced.
        mutations: How many decision-engine mutations were planned.
        cold_starts: How many cold-start trials were planned.
        written: Whether ``chains.env`` was actually written this cycle.
        journaled: How many mutation rows were recorded in the audit log.
    """

    matrix_cells: int
    faults: int
    mutations: int
    cold_starts: int
    written: bool
    journaled: int


def select_healthy_cells(
    probes: Sequence[CellProbeRow],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    ok_fraction: float = DEFAULT_OK_FRACTION,
) -> frozenset[tuple[str, str]]:
    """The ``(provider, model)`` cells observed healthy over the probe window.

    A cell is healthy when it has at least ``min_samples`` probes and the ok
    share clears ``ok_fraction`` — the same bar 3a uses, so cold-start provider
    resolution prefers cells the observatory has actually seen working.
    """
    healthy: set[tuple[str, str]] = set()
    for summary in summarize_cells(probes):
        if cell_is_healthy(summary, min_samples=min_samples, ok_fraction=ok_fraction):
            healthy.add((summary.provider_id, summary.model_id))
    return frozenset(healthy)


def plan_and_apply(
    db_path: Path,
    chains_path: Path,
    *,
    matrix: ProviderModelMatrix,
    ranking: BenchmarkRanking,
    equivalences: EquivalenceTable,
    recorded_at: datetime,
    prior_metric: str | None = None,
    sigma: float = DEFAULT_SIGMA,
    max_mutations: int = DEFAULT_MAX_MUTATIONS,
    enabled: bool = False,
    since: datetime | None = None,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> CycleReport:
    """Decide, plan and (maybe) apply, given an already-swept matrix + probe DB.

    The network-free half of a cycle: read the recent cell probes, attribute
    faults (3a), score the models (2d), decide mutations (3b), plan the shadow
    rewrite (3d-1c) and apply it behind ``enabled`` (3d-2). Pure of network; the
    only I/O is the probe DB read, the audit-log write and the gated chains.env
    write.

    Args:
        db_path: SQLite path for the probe + audit tables.
        chains_path: The authoritative ``chains.env``.
        matrix: The live matrix from this cycle's sweep.
        ranking: The benchmark prior (1c/3d-0).
        equivalences: The name↔canonical↔id resolver (1d).
        recorded_at: One UTC timestamp for this cycle's journal rows.
        prior_metric: The benchmark metric for the cold-start prior, if any.
        sigma: The decision engine's statistical separation factor (3b).
        max_mutations: Hard cap on chain-mutating edits applied this cycle.
        enabled: Write to ``chains.env`` only when ``True``.
        since: Explicit window start; defaults to ``recorded_at - lookback``.
        lookback: The rolling window used when ``since`` is not given, so stale
            probes never mask the current state.

    Returns:
        The :class:`CycleReport`.
    """
    window_start = since if since is not None else recorded_at - lookback
    probes = fetch_cell_probes(db_path, since=window_start)
    faults = attribute_faults(probes, equivalences=equivalences)
    healthy = select_healthy_cells(probes)
    scoreboard = aggregate_model_performance(
        db_path, ranking=ranking, equivalences=equivalences, prior_metric=prior_metric
    )
    mutations = plan_mutations(faults, scoreboard, sigma=sigma)
    content = chains_path.read_text(encoding="utf-8") if chains_path.exists() else ""
    plan = plan_chain_rewrite(
        content,
        mutations,
        ranking=ranking,
        equivalences=equivalences,
        matrix=matrix,
        healthy_cells=healthy,
        max_mutations=max_mutations,
    )
    result = apply_chain_rewrite(
        plan,
        mutations,
        db_path=db_path,
        chains_path=chains_path,
        recorded_at=recorded_at,
        enabled=enabled,
    )
    return CycleReport(
        matrix_cells=len(matrix.cells),
        faults=len(faults),
        mutations=len(mutations),
        cold_starts=len(plan.cold_starts),
        written=result.written,
        journaled=result.journaled,
    )


async def run_autopilot_cycle(
    *,
    settings: Settings,
    client: httpx.AsyncClient,
    db_path: Path,
    chains_path: Path,
    ranking: BenchmarkRanking,
    equivalences: EquivalenceTable,
    recorded_at: datetime,
    prior_metric: str | None = None,
    sigma: float = DEFAULT_SIGMA,
    max_mutations: int = DEFAULT_MAX_MUTATIONS,
    enabled: bool = False,
    since: datetime | None = None,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> CycleReport:
    """Run one full cadence: sweep the matrix + cells, record, then plan/apply.

    The write is armed only when this cycle actually recorded fresh probes
    (``recorded > 0``): an offline / credential-less cycle that swept nothing
    degrades to a shadow run rather than mutating ``chains.env`` from stale data.

    Args:
        settings: Proxy settings (provider endpoints + the apply flag).
        client: An open ``httpx.AsyncClient`` for the sweeps.
        db_path: SQLite path for the probe + audit tables.
        chains_path: The authoritative ``chains.env``.
        ranking: The benchmark prior (1c/3d-0).
        equivalences: The name↔canonical↔id resolver (1d).
        recorded_at: One UTC timestamp for this cycle's probe + journal rows.
        prior_metric: The benchmark metric for the cold-start prior, if any.
        sigma: The decision engine's statistical separation factor (3b).
        max_mutations: Hard cap on chain-mutating edits applied this cycle.
        enabled: Write to ``chains.env`` only when ``True``.
        since: Explicit probe-window start; defaults to ``recorded_at - lookback``.
        lookback: The rolling window for attribution / healthy-cell selection.

    Returns:
        The :class:`CycleReport`.
    """
    matrix = await sweep_model_matrix(settings, client)
    health = await sweep_cell_health(matrix, settings, client)
    recorded = record_cell_probes(db_path, health, recorded_at=recorded_at)
    return plan_and_apply(
        db_path,
        chains_path,
        matrix=matrix,
        ranking=ranking,
        equivalences=equivalences,
        recorded_at=recorded_at,
        prior_metric=prior_metric,
        sigma=sigma,
        max_mutations=max_mutations,
        enabled=enabled and recorded > 0,
        since=since,
        lookback=lookback,
    )
