"""Model-first chains.env generation — assemble, render, atomic write (SP-MFC-GENERATE).

Slice 4 of the model-first chains arc (``docs/model_first_chains_architecture.md``).
Composes slices 1–3 into a regenerated ``chains.env``: gate selection by the
servable set, expand each selected model to its providers, render the three
``MODEL_*`` lines (preserving everything else in the file), and write atomically
behind the same apply flag the chainpilot uses.

Pure and synchronous: the live gathering (AA fetch, async matrix sweep, probe
latency) and the CLI / cadence belong to ``SP-MFC-CHAINPILOT-REFRAME`` (slice 5),
which calls :func:`regenerate` with already-gathered inputs and an injected
``speed_for``. A disabled apply gate makes the write a no-op (shadow by default),
and a missing ``MODEL_*`` slot fails loud rather than appending into an unknown
file structure.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ferova.core.logging import get_logger
from ferova.llm_proxy.providers.aa_ingest import AaRanking
from ferova.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from ferova.llm_proxy.providers.model_matrix import ProviderModelMatrix
from ferova.llm_proxy.routing.chain_expand import (
    build_servable_index,
    expand_chains,
    servable_names,
)
from ferova.llm_proxy.routing.model_select import (
    DEFAULT_DEPTH,
    DEFAULT_MARGIN,
    select_models,
)

_log = get_logger(__name__)

TIER_SLOT: dict[str, str] = {
    "opus": "MODEL_OPUS",
    "sonnet": "MODEL_SONNET",
    "haiku": "MODEL_HAIKU",
}

__all__ = [
    "TIER_SLOT",
    "GenerateError",
    "GenerateResult",
    "assemble_chains",
    "regenerate",
    "render_chains_content",
    "write_chains_env",
]


class GenerateError(Exception):
    """Raised when the current ``chains.env`` lacks a tier's ``MODEL_*`` slot."""


class GenerateResult(BaseModel):
    """Outcome of a regeneration.

    Attributes:
        chains: The assembled per-tier entries that were rendered.
        written: Whether the new content was written to disk.
        changed: Whether the rendered content differs from the current file.
    """

    model_config = ConfigDict(frozen=True)

    chains: dict[str, tuple[str, ...]]
    written: bool
    changed: bool


def assemble_chains(
    ranking: AaRanking,
    matrix: ProviderModelMatrix,
    equivalences: EquivalenceTable,
    *,
    speed_for: Callable[[str, str], float | None],
    margin: float = DEFAULT_MARGIN,
    depth: Mapping[str, int] = DEFAULT_DEPTH,
) -> dict[str, tuple[str, ...]]:
    """Assemble the three tier chains from the gathered inputs.

    Builds the servable index (the matrix∩AA join), gates selection by the
    servable names (matrix-before-top-N), and expands each selected model into
    ordered ``provider/model`` entries with the ``claude_code`` tail.

    Args:
        ranking: The collapsed capability ranking (slice 1).
        matrix: The live ``(provider × model)`` matrix.
        equivalences: The benchmark name↔id resolver.
        speed_for: ``(provider_id, model_id) -> latency_s | None``.
        margin: Index points of tolerance below each Claude anchor.
        depth: The per-tier chain depth.

    Returns:
        ``{tier: ("provider/model", ..., "claude_code/<tier>")}``.
    """
    index = build_servable_index(matrix, equivalences)
    selections = select_models(ranking, servable=servable_names(index), margin=margin, depth=depth)
    return expand_chains(selections, index=index, speed_for=speed_for)


def render_chains_content(content: str, chains: Mapping[str, Sequence[str]]) -> str:
    """Replace the three ``MODEL_*`` lines, preserving every other line.

    Args:
        content: The current ``chains.env`` text.
        chains: ``{tier: (entry, ...)}`` to render.

    Returns:
        The new file text.

    Raises:
        GenerateError: When a tier's ``MODEL_*`` slot is absent from ``content``.
    """
    lines = content.split("\n")
    found: set[str] = set()
    for position, line in enumerate(lines):
        for tier, slot in TIER_SLOT.items():
            if line.startswith(f"{slot}="):
                lines[position] = f"{slot}={','.join(chains[tier])}"
                found.add(tier)
    missing = set(TIER_SLOT) - found
    if missing:
        raise GenerateError(f"chains.env missing slot(s): {sorted(missing)}")
    return "\n".join(lines)


def write_chains_env(new_content: str, chains_path: Path, *, enabled: bool) -> bool:
    """Atomically write ``new_content`` to ``chains_path`` when enabled.

    Stages to a sibling temp file, backs the current file up to ``<name>.bak``,
    then ``os.replace`` swaps it in (atomic against concurrent readers). A
    disabled call is a no-op.

    Args:
        new_content: The rendered file text.
        chains_path: The target file.
        enabled: The apply gate; ``False`` makes this a no-op.

    Returns:
        ``True`` if the file was written, ``False`` if gated off.
    """
    if not enabled:
        _log.info("mfc_generate_shadow", chains_path=str(chains_path))
        return False
    tmp_path = chains_path.parent / f"{chains_path.name}.tmp"
    backup_path = chains_path.parent / f"{chains_path.name}.bak"
    if chains_path.exists():
        backup_path.write_text(chains_path.read_text(encoding="utf-8"), encoding="utf-8")
    tmp_path.write_text(new_content, encoding="utf-8")
    os.replace(tmp_path, chains_path)
    _log.info("mfc_generate_written", chains_path=str(chains_path))
    return True


def regenerate(
    current_content: str,
    ranking: AaRanking,
    matrix: ProviderModelMatrix,
    equivalences: EquivalenceTable,
    *,
    speed_for: Callable[[str, str], float | None],
    chains_path: Path,
    enabled: bool,
    margin: float = DEFAULT_MARGIN,
    depth: Mapping[str, int] = DEFAULT_DEPTH,
) -> GenerateResult:
    """Assemble, render, and (if enabled and changed) write a new ``chains.env``.

    Args:
        current_content: The current ``chains.env`` text.
        ranking, matrix, equivalences, speed_for: The gathered inputs.
        chains_path: The file to write.
        enabled: The apply gate; the write is a no-op when ``False``.
        margin, depth: The selection knobs.

    Returns:
        A :class:`GenerateResult` recording the chains, whether it changed, and
        whether it was written.

    Raises:
        GenerateError: A tier slot is absent from ``current_content``.
        SelectError: An anchor model is absent from the ranking.
    """
    chains = assemble_chains(
        ranking, matrix, equivalences, speed_for=speed_for, margin=margin, depth=depth
    )
    new_content = render_chains_content(current_content, chains)
    changed = new_content != current_content
    written = write_chains_env(new_content, chains_path, enabled=enabled and changed)
    return GenerateResult(chains=chains, written=written, changed=changed)
