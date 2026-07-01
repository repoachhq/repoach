"""Effort-aware probe sweep (SP-CHAINPILOT-EFFORT-SWEEP, Phase 2a-3-ii).

Where the 2a-2 health sweep probes every matrix cell with no reasoning knob
(baseline alive / latency), this sweep probes every cell **with the cell's
reasoning-effort knob applied** — the single-pass policy, each provider's
declared ``min_effort`` (2a-3-i) — and tags each observation with the effort it
requested. The reasoned-at-effort time-series it feeds (``cell_effort_probe``,
:mod:`effort_probe_store`) is what the effort resolver (2a-3-iii) reads to learn
which ``(provider, model)`` cells reason and at which effort.

Reasoning effort is per-provider under the single-pass policy, so this needs no
copy of the 2a-2 orchestration: it slices the matrix into per-provider
sub-matrices and calls :func:`sweep_cell_health` once per provider with that
provider's :func:`effort_extra_body` as the uniform ``extra_body``.
``matrix.cells`` is already provider-grouped (``assemble_matrix`` flattens listing
by listing), so iterating :meth:`ProviderModelMatrix.providers` and concatenating
the per-provider results reproduces ``matrix.cells`` order — the 1:1 contract
2a-2 guarantees is preserved.

It never raises (``sweep_cell_health`` never raises) and performs no I/O beyond
the injected client. It is additive and unwired — no schedule or CLI runs it yet
(the cadence is 3e); the caller wires sweep → record, as 2a-2's cadence does.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import httpx

from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.cell_probe import CellHealth
from ferova.llm_proxy.providers.cell_probe_sweep import sweep_cell_health
from ferova.llm_proxy.providers.effort_knob import effort_extra_body, probe_effort_for
from ferova.llm_proxy.providers.model_matrix import ProviderModelMatrix
from ferova.llm_proxy.providers.registry import (
    PROVIDER_DESCRIPTORS,
    ProviderDescriptor,
)

_PROBE_PROMPT = "Reply with the single word: ok"

__all__ = ["EffortProbe", "sweep_effort_health"]


@dataclass(frozen=True, slots=True)
class EffortProbe:
    """One cell probed with its reasoning-effort knob applied.

    Attributes:
        health: The per-cell observation (:class:`CellHealth`) — status, latency,
            and the reasoning/answer character counts.
        effort_used: The effort value requested for this probe (the provider's
            ``min_effort``), or ``None`` for a provider with no effort knob.
    """

    health: CellHealth
    effort_used: str | None

    @property
    def model_used(self) -> str:
        """The model actually probed.

        Equal to ``health.model_id``: a direct cell probe has no routing
        indirection, so policy B's ``model_used`` mirrors the cell's model.
        """
        return self.health.model_id


def _provider_submatrix(matrix: ProviderModelMatrix, provider_id: str) -> ProviderModelMatrix:
    """Return *matrix* restricted to *provider_id*'s cells (listings preserved)."""
    cells = tuple(cell for cell in matrix.cells if cell.provider_id == provider_id)
    return ProviderModelMatrix(cells=cells, listings=matrix.listings)


async def sweep_effort_health(
    matrix: ProviderModelMatrix,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    descriptors: Mapping[str, ProviderDescriptor] = PROVIDER_DESCRIPTORS,
    max_concurrency: int = 8,
    prompt: str = _PROBE_PROMPT,
    max_tokens: int = 64,
    timeout_s: float = 30.0,
    slow_threshold_s: float = 8.0,
) -> list[EffortProbe]:
    """Probe every cell of *matrix* with its per-provider effort knob. Never raises.

    For each provider in *matrix*, resolve its single-pass effort, build the
    effort fragment, and delegate to :func:`sweep_cell_health` on that provider's
    sub-matrix with the fragment as ``extra_body``. Wrap each returned
    :class:`CellHealth` in an :class:`EffortProbe` tagging the effort requested.

    Args:
        matrix: The ``(provider × model)`` matrix to probe (1b).
        settings: Proxy settings, source of base-url overrides and keys.
        client: A caller-owned ``httpx.AsyncClient``.
        descriptors: The provider descriptor table (defaults to the full
            :data:`PROVIDER_DESCRIPTORS`; injectable for tests).
        max_concurrency: Maximum simultaneous probes within each provider sweep.
        prompt: The probe user message.
        max_tokens: Per-probe completion budget.
        timeout_s: Per-probe hard cap.
        slow_threshold_s: Latency above which a probe is reported ``slow``.

    Returns:
        One :class:`EffortProbe` per cell, in ``matrix.cells`` order.
    """
    probes: list[EffortProbe] = []
    for provider_id in matrix.providers():
        effort = probe_effort_for(provider_id)
        extra_body = effort_extra_body(provider_id, effort)
        sub = _provider_submatrix(matrix, provider_id)
        healths = await sweep_cell_health(
            sub,
            settings,
            client,
            descriptors=descriptors,
            max_concurrency=max_concurrency,
            extra_body=extra_body or None,
            prompt=prompt,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
            slow_threshold_s=slow_threshold_s,
        )
        probes.extend(EffortProbe(health=health, effort_used=effort) for health in healths)
    return probes
