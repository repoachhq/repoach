"""Fault attribution classifier (SP-CHAINPILOT-ATTRIBUTION).

Phase 3a of the Chain Autopilot arc — the hard conceptual core (Principle 5,
attribution before eviction), isolated as a pure classifier. Given the matrix's
recent per-cell probe observations, it decides for each failing
``(provider, model)`` cell whether the fault is the model's (evict everywhere),
the provider's (skip that provider for that model), or ours (fix our handling).
It acts on nothing — the decision engine (3b) does.

The engine of the model-vs-provider split is the **cross-provider comparison**:
the same model healthy on one provider but failing on another is that
provider's fault for the model; failing across providers is the model's. Cells
are joined across providers by **canonical** model (1d equivalences) — a model is
exposed under provider-specific ids, so grouping by the raw id would make every
cell look single-provider and turn a provider-local fault into a model-wide
eviction (the post-incident Fix-3). A model-wide eviction (``MODEL_FAULT``)
requires the model observed dead on **≥2 distinct providers**; a single observed
provider yields only a ``PROVIDER_FAULT`` (drop that ref) — so an imperfect
equivalence join can never over-evict on one provider's evidence, only
under-evict (lower harm, capped and reversible).
A probe is **starved** when it reasoned but produced no visible content
(``reasoning_chars > 0 and content_chars == 0``) — the post-Phase-0 signature of
*our* output budget being too tight, not a bad model or provider. Per the
operator's decision, a starved cell is still examined across providers so its
verdict carries a **scope** (systemic vs local) that enables a definitive fix
rather than a blind label. **Slowness is not a fault** — a slow probe still
returned *content*, so it counts as healthy; only ``dead``/``starved`` drive a
verdict. (Latency is itself a quality signal; acting on it — a latency-demote —
is a deferred follow-up, not yet wired.) A hard ``ReadTimeout``/error is a
``dead`` probe, not ``slow``: a head that times out for most of the window is
still evictable (correctly — it is unusable), while a transient slow or
occasional timeout in an otherwise-healthy window no longer flips a working head
out. Verdicts aggregate over a window of probes with a minimum sample size
(Principle 6); below it they are inconclusive.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from ferova.health.model_health import STATUS_EMPTY, STATUS_OK, STATUS_SLOW
from ferova.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from ferova.llm_proxy.providers.cell_probe_store import CellProbeRow

DEFAULT_MIN_SAMPLES: int = 3
"""Minimum probes for a cell before its fault is judged (Principle 6)."""

DEFAULT_OK_FRACTION: float = 0.5
"""Fraction of probes that must have returned content (``ok`` or ``slow``) for a
cell to count as healthy."""

__all__ = [
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_OK_FRACTION",
    "CellFault",
    "CellHealthSummary",
    "FaultClass",
    "FaultScope",
    "attribute_faults",
    "cell_is_healthy",
    "summarize_cells",
]


class FaultClass(StrEnum):
    """The attributed fault for a cell."""

    HEALTHY = "healthy"
    OUR_FAULT = "our_fault"
    PROVIDER_FAULT = "provider_fault"
    MODEL_FAULT = "model_fault"
    INCONCLUSIVE = "inconclusive"


class FaultScope(StrEnum):
    """How widely the fault reaches across the model's providers."""

    NONE = "none"
    LOCAL = "local"
    SYSTEMIC = "systemic"


@dataclass(frozen=True)
class CellHealthSummary:
    """Aggregated probe outcomes for one ``(provider, model)`` cell.

    Attributes:
        provider_id: The provider the cell was probed on.
        model_id: The probed model id.
        n_samples: Total probes summarized.
        n_ok: Probes that returned content within latency.
        n_slow: Probes that returned content but breached the latency bar.
        n_starved: Probes that reasoned but returned no content (our budget).
        n_dead: Probes that errored, or returned empty with no reasoning.
    """

    provider_id: str
    model_id: str
    n_samples: int
    n_ok: int
    n_slow: int
    n_starved: int
    n_dead: int


@dataclass(frozen=True)
class CellFault:
    """The attribution verdict for one cell.

    Attributes:
        provider_id: The provider the verdict is about.
        model_id: The model the verdict is about.
        fault: The attributed :class:`FaultClass`.
        scope: How widely the fault reaches (:class:`FaultScope`).
        reason: A short human-readable justification.
        n_samples: The probes the verdict was drawn from.
    """

    provider_id: str
    model_id: str
    fault: FaultClass
    scope: FaultScope
    reason: str
    n_samples: int


def summarize_cells(probes: Sequence[CellProbeRow]) -> list[CellHealthSummary]:
    """Aggregate probe rows into one :class:`CellHealthSummary` per cell.

    Args:
        probes: The probe observations to summarize (any window the caller
            selected); order is irrelevant.

    Returns:
        One summary per ``(provider, model)`` cell, ordered by
        ``(provider_id, model_id)``.
    """
    ok: dict[tuple[str, str], int] = defaultdict(int)
    slow: dict[tuple[str, str], int] = defaultdict(int)
    starved: dict[tuple[str, str], int] = defaultdict(int)
    dead: dict[tuple[str, str], int] = defaultdict(int)
    keys: set[tuple[str, str]] = set()
    for probe in probes:
        key = (probe.provider_id, probe.model_id)
        keys.add(key)
        if probe.status == STATUS_OK:
            ok[key] += 1
        elif probe.status == STATUS_SLOW:
            slow[key] += 1
        elif probe.status == STATUS_EMPTY and probe.reasoning_chars > 0:
            starved[key] += 1
        else:
            dead[key] += 1

    summaries: list[CellHealthSummary] = []
    for provider_id, model_id in sorted(keys):
        key = (provider_id, model_id)
        n_ok, n_slow, n_starved, n_dead = ok[key], slow[key], starved[key], dead[key]
        summaries.append(
            CellHealthSummary(
                provider_id=provider_id,
                model_id=model_id,
                n_samples=n_ok + n_slow + n_starved + n_dead,
                n_ok=n_ok,
                n_slow=n_slow,
                n_starved=n_starved,
                n_dead=n_dead,
            )
        )
    return summaries


def cell_is_healthy(
    summary: CellHealthSummary,
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    ok_fraction: float = DEFAULT_OK_FRACTION,
) -> bool:
    """Whether a cell clears the responded-with-content bar with enough samples.

    The single definition of cell health, shared by attribution (3a) and the
    autopilot loop's healthy-cell selection (3e) so the two cannot drift. ``slow``
    probes count as healthy alongside ``ok``: a slow response still returned
    content, so it is never alone grounds to evict a working model. A hard
    timeout/error is a ``dead`` probe (not ``slow``); a transient one in an
    otherwise-healthy window no longer flips a head out, but a window dominated by
    dead probes still fails the bar.
    """
    if summary.n_samples < min_samples:
        return False
    return ((summary.n_ok + summary.n_slow) / summary.n_samples) >= ok_fraction


def _is_healthy(summary: CellHealthSummary, *, ok_fraction: float, min_samples: int) -> bool:
    """Internal alias of :func:`cell_is_healthy` (positional-history call sites)."""
    return cell_is_healthy(summary, min_samples=min_samples, ok_fraction=ok_fraction)


def _model_key(model_id: str, equivalences: EquivalenceTable | None) -> str:
    """The cross-provider grouping key for a model.

    The same model is exposed under provider-specific ids (``deepseek-ai/...`` on
    NIM, ``deepseek/...`` on OpenRouter, ``deepseek-v4-pro`` direct), so grouping
    by the raw id would make every cell look single-provider and turn a
    provider-local fault into a model-wide eviction. The canonical (1d) joins them
    so the model-vs-provider split sees the model's true cross-provider profile.
    Falls back to the raw id when no equivalence table is supplied.
    """
    if equivalences is None:
        return model_id
    return equivalences.canonical_for_model_id(model_id) or model_id


def _dominant_failure(summary: CellHealthSummary) -> str:
    """The largest genuine-failure bucket; ties resolve starved > dead.

    ``slow`` is not a failure (it counts as healthy in :func:`cell_is_healthy`),
    so it is excluded — an unhealthy cell is always attributed to its real fault
    (our budget = starved, or the model/provider = dead), never to latency.
    Precondition: call only on an unhealthy cell (``n_starved + n_dead >= 1``);
    on an all-zero summary it defaults to ``starved`` (the conservative,
    no-eviction verdict).
    """
    ranked = sorted(
        (("starved", summary.n_starved), ("dead", summary.n_dead)),
        key=lambda item: item[1],
        reverse=True,
    )
    return ranked[0][0]


def attribute_faults(
    probes: Sequence[CellProbeRow],
    *,
    min_samples: int = DEFAULT_MIN_SAMPLES,
    ok_fraction: float = DEFAULT_OK_FRACTION,
    equivalences: EquivalenceTable | None = None,
) -> list[CellFault]:
    """Classify each cell's fault from a window of probe observations.

    Pure and I/O-free: the caller injects the probes (windowed via
    ``fetch_cell_probes``). The cross-provider profile of each model is always
    computed, so a starved cell's verdict carries a systemic-vs-local scope
    rather than a blind label.

    Args:
        probes: The probe observations to classify.
        min_samples: Minimum probes before a cell is judged; below it the
            verdict is ``inconclusive``.
        ok_fraction: The ok-share a cell needs to count as healthy.
        equivalences: Optional name↔canonical resolver (1d). When given, cells are
            grouped across providers by canonical model so the model-vs-provider
            split sees the model's true cross-provider profile; without it, the
            raw provider-specific id is used (single-provider grouping).

    Returns:
        One :class:`CellFault` per cell, ordered by ``(provider_id, model_id)``.
    """
    summaries = summarize_cells(probes)
    by_model: dict[str, list[CellHealthSummary]] = defaultdict(list)
    for summary in summaries:
        by_model[_model_key(summary.model_id, equivalences)].append(summary)

    out: list[CellFault] = []
    for summary in summaries:
        if summary.n_samples < min_samples:
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.INCONCLUSIVE,
                    FaultScope.NONE,
                    f"only {summary.n_samples} probe(s) (< {min_samples})",
                    summary.n_samples,
                )
            )
            continue
        if _is_healthy(summary, ok_fraction=ok_fraction, min_samples=min_samples):
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.HEALTHY,
                    FaultScope.NONE,
                    "ok share clears the bar",
                    summary.n_samples,
                )
            )
            continue

        siblings = by_model[_model_key(summary.model_id, equivalences)]
        mode = _dominant_failure(summary)
        if mode == "starved":
            sampled = [s for s in siblings if s.n_samples >= min_samples]
            systemic = all(
                not _is_healthy(s, ok_fraction=ok_fraction, min_samples=min_samples)
                and _dominant_failure(s) == "starved"
                for s in sampled
            )
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.OUR_FAULT,
                    FaultScope.SYSTEMIC if systemic else FaultScope.LOCAL,
                    "reasoned but returned no content — output budget too tight",
                    summary.n_samples,
                )
            )
            continue

        ok_elsewhere = any(
            s.provider_id != summary.provider_id
            and _is_healthy(s, ok_fraction=ok_fraction, min_samples=min_samples)
            for s in siblings
        )
        dead_providers = {
            s.provider_id
            for s in siblings
            if s.n_samples >= min_samples
            and not _is_healthy(s, ok_fraction=ok_fraction, min_samples=min_samples)
            and _dominant_failure(s) == "dead"
        }
        if ok_elsewhere:
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.PROVIDER_FAULT,
                    FaultScope.LOCAL,
                    f"{mode} here but the model is healthy on another provider",
                    summary.n_samples,
                )
            )
        elif len(dead_providers) < 2:
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.PROVIDER_FAULT,
                    FaultScope.LOCAL,
                    f"{mode} here but fewer than two providers are conclusively dead "
                    "— dropping this ref, not evicting the model",
                    summary.n_samples,
                )
            )
        else:
            out.append(
                CellFault(
                    summary.provider_id,
                    summary.model_id,
                    FaultClass.MODEL_FAULT,
                    FaultScope.SYSTEMIC,
                    f"{mode} on every provider serving the model",
                    summary.n_samples,
                )
            )
    return out
