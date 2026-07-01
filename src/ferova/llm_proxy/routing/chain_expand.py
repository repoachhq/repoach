"""Model-first provider expansion — NIM-first, then live speed (SP-MFC-EXPAND).

Slice 3 of the model-first chains arc (``docs/model_first_chains_architecture.md``).
The join linchpin: it expands each selected model (``SP-MFC-SELECT``) into every
provider that serves it (the ``(provider × model)`` matrix joined through the
benchmark name↔id equivalence table), ordered NIM-first (free) then by
live-measured probe latency. It also derives the **servable** name set that gates
selection (matrix-before-top-N), which ``SP-MFC-GENERATE`` feeds back into SELECT.

Pure and I/O-free: the matrix, the equivalence table, and a ``speed_for`` latency
lookup are all injected, so this leaf keeps no edge to the probe store and stays
trivially testable. The equivalence table is the authoritative join — a cell
whose model id matches no alias is dropped and counted (logged), never matched by
a fuzzy guess; closing a coverage gap is a data fix in the equivalence table.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence

from ferova.core.logging import get_logger
from ferova.llm_proxy.providers.aa_ingest import ModelCapability, normalize_model_name
from ferova.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from ferova.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix

_log = get_logger(__name__)

NIM_PROVIDER = "nvidia_nim"
NIM_HEAD_TIERS = frozenset({"sonnet", "haiku"})

__all__ = [
    "NIM_HEAD_TIERS",
    "NIM_PROVIDER",
    "build_servable_index",
    "expand_chains",
    "expand_tier",
    "order_cells",
    "servable_names",
]


def build_servable_index(
    matrix: ProviderModelMatrix,
    equivalences: EquivalenceTable,
) -> dict[str, tuple[ModelCell, ...]]:
    """Map each normalized AA model name to the matrix cells that serve it.

    A cell is attached under the normalized form of every benchmark alias its
    model id resolves to (``aliases_for_model_id``). Cells whose model id matches
    no alias are dropped and counted — the equivalence table is the authoritative
    join, so a coverage gap stays visible rather than papered over.

    Args:
        matrix: The live ``(provider × model)`` matrix.
        equivalences: The benchmark name↔id resolver.

    Returns:
        ``{normalized_name: (ModelCell, ...)}``, cells in matrix order.
    """
    index: dict[str, list[ModelCell]] = {}
    unmatched = 0
    for cell in matrix.cells:
        aliases = equivalences.aliases_for_model_id(cell.model_id)
        if not aliases:
            unmatched += 1
            continue
        for alias in aliases:
            index.setdefault(normalize_model_name(alias), []).append(cell)
    _log.info(
        "mfc_servable_index",
        names=len(index),
        cells=len(matrix.cells),
        unmatched_cells=unmatched,
    )
    return {name: tuple(cells) for name, cells in index.items()}


def servable_names(index: Mapping[str, tuple[ModelCell, ...]]) -> frozenset[str]:
    """The normalized names at least one provider serves (the selection gate)."""
    return frozenset(index)


def _cell_sort_key(
    cell: ModelCell,
    speed_for: Callable[[str, str], float | None],
    nim_provider: str,
) -> tuple[int, float]:
    """Sort key ``(tier, latency)`` — NIM block first, then fastest-first.

    NIM cells get rank ``0`` (free, always ahead); everything else rank ``1``.
    Within a rank, ascending probe latency orders fastest first; an unmeasured
    latency becomes infinity so it sorts last.
    """
    rank = 0 if cell.provider_id == nim_provider else 1
    latency = speed_for(cell.provider_id, cell.model_id)
    return (rank, latency if latency is not None else math.inf)


def order_cells(
    cells: Sequence[ModelCell],
    *,
    speed_for: Callable[[str, str], float | None],
    nim_provider: str = NIM_PROVIDER,
) -> tuple[ModelCell, ...]:
    """Order a model's serving cells NIM-first, then by ascending latency.

    Args:
        cells: The cells serving one model.
        speed_for: ``(provider_id, model_id) -> latency_s | None``.
        nim_provider: The free provider id that heads every block.

    Returns:
        The cells ordered head→tail.
    """
    return tuple(sorted(cells, key=lambda cell: _cell_sort_key(cell, speed_for, nim_provider)))


def _promote_nim_head(entries: list[str], *, tier: str, nim_provider: str) -> list[str]:
    """Move the first NIM cell to the head, preserving the rest of the order.

    Model-first selection is capability-ranked, so a tier whose top-capability
    model is not NIM-served gets a paid head — a cost regression on the
    high-volume sonnet/haiku tiers that breaks the "NIM is the free head of
    sonnet + haiku" invariant. Because the entries are already in
    capability-then-NIM-first order, the first NIM cell is the highest-capability
    NIM-served model; promoting it to the head restores the invariant. If no NIM
    cell serves the tier the capability head is kept and the gap is logged.
    """
    prefix = f"{nim_provider}/"
    if entries and entries[0].startswith(prefix):
        return entries
    nim_index = next((i for i, ref in enumerate(entries) if ref.startswith(prefix)), None)
    if nim_index is None:
        _log.warning("mfc_nim_head_unavailable", tier=tier)
        return entries
    entries.insert(0, entries.pop(nim_index))
    return entries


def expand_tier(
    models: Sequence[ModelCapability],
    *,
    index: Mapping[str, tuple[ModelCell, ...]],
    speed_for: Callable[[str, str], float | None],
    tier: str,
    nim_provider: str = NIM_PROVIDER,
    nim_head_tiers: frozenset[str] = NIM_HEAD_TIERS,
) -> tuple[str, ...]:
    """Expand a tier's selected models into ordered ``provider/model`` entries.

    Each selected model is expanded to its ordered serving cells; entries are
    de-duplicated (first occurrence wins) and the ``claude_code/<tier>`` tail is
    appended as the net. A selected model absent from the index is skipped and
    logged (its capability is still covered by the tail). For a tier in
    ``nim_head_tiers`` a free NIM cell is promoted to the head (see
    :func:`_promote_nim_head`) so the high-volume tiers never lead with a paid
    provider; ``opus`` is exempt — it is the paid frontier tier by design.

    Args:
        models: The tier's selected models, head→tail.
        index: The servable index from :func:`build_servable_index`.
        speed_for: ``(provider_id, model_id) -> latency_s | None``.
        tier: The capability tier (``opus`` / ``sonnet`` / ``haiku``).
        nim_provider: The free provider id that heads every model's block.
        nim_head_tiers: The tiers whose chain head must be a NIM cell.

    Returns:
        The ordered chain entries, ending with ``claude_code/<tier>``.
    """
    entries: list[str] = []
    seen: set[str] = set()
    for model in models:
        cells = index.get(model.name, ())
        if not cells:
            _log.info("mfc_expand_model_dropped", model=model.name, tier=tier)
            continue
        for cell in order_cells(cells, speed_for=speed_for, nim_provider=nim_provider):
            ref = f"{cell.provider_id}/{cell.model_id}"
            if ref not in seen:
                seen.add(ref)
                entries.append(ref)
    if tier in nim_head_tiers:
        entries = _promote_nim_head(entries, tier=tier, nim_provider=nim_provider)
    entries.append(f"claude_code/{tier}")
    return tuple(entries)


def expand_chains(
    selections: Mapping[str, Sequence[ModelCapability]],
    *,
    index: Mapping[str, tuple[ModelCell, ...]],
    speed_for: Callable[[str, str], float | None],
    nim_provider: str = NIM_PROVIDER,
    nim_head_tiers: frozenset[str] = NIM_HEAD_TIERS,
) -> dict[str, tuple[str, ...]]:
    """Expand every tier's selections into ordered chain entries.

    Args:
        selections: ``{tier: (ModelCapability, ...)}`` from SELECT.
        index: The servable index from :func:`build_servable_index`.
        speed_for: ``(provider_id, model_id) -> latency_s | None``.
        nim_provider: The free provider id that heads every model's block.
        nim_head_tiers: The tiers whose chain head must be a NIM cell.

    Returns:
        ``{tier: ("provider/model", ..., "claude_code/<tier>")}``.
    """
    return {
        tier: expand_tier(
            models,
            index=index,
            speed_for=speed_for,
            tier=tier,
            nim_provider=nim_provider,
            nim_head_tiers=nim_head_tiers,
        )
        for tier, models in selections.items()
    }
