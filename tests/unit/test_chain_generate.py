"""Unit suite for model-first chains.env generation (SP-MFC-GENERATE).

Covers the pure assembly (slices 1-3 composed), MODEL_* line rendering with
preservation, the atomic flag-gated write, and change detection.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ferova.llm_proxy.providers.aa_ingest import (
    AaRanking,
    ModelCapability,
    normalize_model_name,
)
from ferova.llm_proxy.providers.benchmark_equivalences import (
    EquivalenceTable,
    ModelEquivalence,
)
from ferova.llm_proxy.providers.model_matrix import ModelCell, ProviderModelMatrix
from ferova.llm_proxy.routing.chain_generate import (
    GenerateError,
    assemble_chains,
    regenerate,
    render_chains_content,
    write_chains_env,
)

_CHAINS = "\n".join(
    [
        "# header comment",
        "MODEL_OPUS=old/opus",
        "MODEL_SONNET=old/sonnet",
        "# mid comment",
        "MODEL_HAIKU=old/haiku",
        "OTHER=keep-me",
        "",
    ]
)


def _cap(display: str, capability: float) -> ModelCapability:
    """A ModelCapability keyed on the normalized display name."""
    return ModelCapability(
        name=normalize_model_name(display),
        capability=capability,
        coding=None,
        cheapest_input=None,
        fastest_tps=None,
        variants=(),
    )


def _ranking_with_servable_opus() -> AaRanking:
    """A ranking with the three Claude anchors plus an opus-eligible model."""
    return AaRanking(
        index_version="v4.1",
        models=(
            _cap("Claude Opus 4.7", 53.5),
            _cap("Claude Sonnet 4.6", 47.2),
            _cap("Claude 4.5 Haiku", 29.6),
            _cap("Alpha Model", 50.0),
        ),
    )


def _matrix_alpha_on_nim() -> ProviderModelMatrix:
    """A matrix serving Alpha Model on NIM only."""
    return ProviderModelMatrix(cells=(ModelCell("nvidia_nim", "x/alpha-model"),), listings=())


def _equivalences() -> EquivalenceTable:
    """Maps the Alpha Model benchmark name to its id-pattern token."""
    return EquivalenceTable(
        equivalences=(
            ModelEquivalence(
                canonical="alpha", aliases=("Alpha Model",), id_patterns=("alphamodel",)
            ),
        )
    )


def _no_speed(provider_id: str, model_id: str) -> float | None:
    """A speed lookup with no measurements."""
    return None


def test_assemble_chains_nim_head_and_tail() -> None:
    """opus expands its servable model NIM-first and ends with the claude_code tail."""
    chains = assemble_chains(
        _ranking_with_servable_opus(),
        _matrix_alpha_on_nim(),
        _equivalences(),
        speed_for=_no_speed,
    )
    assert chains["opus"] == ("nvidia_nim/x/alpha-model", "claude_code/opus")
    assert chains["sonnet"] == ("claude_code/sonnet",)
    assert chains["haiku"][-1] == "claude_code/haiku"


def test_render_replaces_only_model_lines() -> None:
    """Rendering rewrites the three MODEL_* values and preserves all else."""
    chains = {
        "opus": ("nvidia_nim/a", "open_router/b"),
        "sonnet": ("nvidia_nim/c",),
        "haiku": ("claude_code/haiku",),
    }
    out = render_chains_content(_CHAINS, chains)
    lines = out.split("\n")
    assert "MODEL_OPUS=nvidia_nim/a,open_router/b" in lines
    assert "MODEL_SONNET=nvidia_nim/c" in lines
    assert "MODEL_HAIKU=claude_code/haiku" in lines
    assert "# header comment" in lines
    assert "# mid comment" in lines
    assert "OTHER=keep-me" in lines


def test_render_missing_slot_raises() -> None:
    """A content without a tier's MODEL_* slot fails loud."""
    content = "MODEL_OPUS=x/y\nMODEL_SONNET=x/y\n"
    with pytest.raises(GenerateError):
        render_chains_content(content, {"opus": ("a/b",), "sonnet": ("c/d",), "haiku": ("e/f",)})


def test_write_gated_off_is_noop(tmp_path: Path) -> None:
    """A disabled write leaves the file untouched and returns False."""
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    written = write_chains_env("NEW CONTENT", target, enabled=False)
    assert written is False
    assert target.read_text(encoding="utf-8") == _CHAINS


def test_write_enabled_writes_and_backs_up(tmp_path: Path) -> None:
    """An enabled write swaps the file in and leaves a .bak of the prior content."""
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    written = write_chains_env("NEW CONTENT", target, enabled=True)
    assert written is True
    assert target.read_text(encoding="utf-8") == "NEW CONTENT"
    assert (tmp_path / "chains.env.bak").read_text(encoding="utf-8") == _CHAINS


def test_regenerate_reports_changed_and_writes(tmp_path: Path) -> None:
    """regenerate renders, detects a change, and writes when enabled."""
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    result = regenerate(
        _CHAINS,
        _ranking_with_servable_opus(),
        _matrix_alpha_on_nim(),
        _equivalences(),
        speed_for=_no_speed,
        chains_path=target,
        enabled=True,
    )
    assert result.changed is True
    assert result.written is True
    assert "MODEL_OPUS=nvidia_nim/x/alpha-model,claude_code/opus" in target.read_text(
        encoding="utf-8"
    )


def test_regenerate_idempotent_reports_unchanged(tmp_path: Path) -> None:
    """Regenerating onto already-current content reports changed=False, no write."""
    target = tmp_path / "chains.env"
    target.write_text(_CHAINS, encoding="utf-8")
    first = regenerate(
        _CHAINS,
        _ranking_with_servable_opus(),
        _matrix_alpha_on_nim(),
        _equivalences(),
        speed_for=_no_speed,
        chains_path=target,
        enabled=True,
    )
    current = target.read_text(encoding="utf-8")
    second = regenerate(
        current,
        _ranking_with_servable_opus(),
        _matrix_alpha_on_nim(),
        _equivalences(),
        speed_for=_no_speed,
        chains_path=target,
        enabled=True,
    )
    assert first.written is True
    assert second.changed is False
    assert second.written is False
