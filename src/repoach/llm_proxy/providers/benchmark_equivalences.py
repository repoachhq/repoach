"""Equivalences — resolve benchmark names to provider model IDs (SP-CHAINPILOT-EQUIVALENCES).

Phase 1d of the Chain Autopilot arc. A pure resolver bridging the two naming
worlds the benchmark prior straddles: the sources' model *names* (1c — e.g.
``"DeepSeek V4 Pro (Max)"``) and the providers' real model *IDs* (1b — e.g.
``deepseek-ai/deepseek-v4-pro``), which differ per source and per provider.

The mapping is a hand-curated, version-controlled resource
(``benchmark_equivalences.json``). To avoid hardcoding exact provider IDs that
cannot be verified without a live sweep, each identity carries ``id_patterns``
— normalised matching tokens (a curated linking *rule*, not fabricated data) —
plus the real ``aliases`` (benchmark names verbatim from the 1c snapshot). The
resolver normalises a model ID (case- and separator-insensitive) and matches it
against the patterns.

It is deliberately a pure string↔string resolver: it imports neither
``ModelCell`` (1b) nor ``BenchmarkEntry`` (1c). The composition — matrix cell →
aliases → prior entries — belongs to 2d, which depends on all three.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from pydantic import BaseModel, ConfigDict

_RESOURCE = Path(__file__).parent / "benchmark_equivalences.json"
_NON_ALNUM = re.compile(r"[^a-z0-9]")

__all__ = [
    "EquivalenceTable",
    "ModelEquivalence",
    "load_equivalence_table",
    "parse_equivalence_table",
]


def _normalize(text: str) -> str:
    """Lower-case ``text`` and strip every non-alphanumeric character.

    Makes matching robust to separators and case, so
    ``deepseek-ai/deepseek-v4-pro`` and the pattern ``deepseek-v4-pro`` collapse
    to a common form.
    """
    return _NON_ALNUM.sub("", text.lower())


class ModelEquivalence(BaseModel):
    """One model identity across benchmark names and provider-ID patterns.

    Attributes:
        canonical: Our stable key for the model.
        aliases: The benchmark names (verbatim from the 1c snapshot) this model
            is ranked under.
        id_patterns: Normalised matching tokens; a provider model ID maps here
            when one of these is a substring of its normalised form.
    """

    model_config = ConfigDict(frozen=True)

    canonical: str
    aliases: tuple[str, ...]
    id_patterns: tuple[str, ...]


class EquivalenceTable(BaseModel):
    """The curated benchmark-name ↔ provider-ID mapping, with pure resolvers."""

    model_config = ConfigDict(frozen=True)

    equivalences: tuple[ModelEquivalence, ...]

    def _match_model_id(self, model_id: str) -> ModelEquivalence | None:
        """The equivalence whose longest pattern is a substring of the id.

        Most-specific-wins: when several patterns match (e.g. a broad family token
        and a precise SKU token), the longest matching pattern decides, so sibling
        SKUs are not collapsed onto a broader entry. Returns ``None`` if nothing
        matches.
        """
        norm = _normalize(model_id)
        best: ModelEquivalence | None = None
        best_len = 0
        for equivalence in self.equivalences:
            for pattern in equivalence.id_patterns:
                normalized = _normalize(pattern)
                if normalized in norm and len(normalized) > best_len:
                    best, best_len = equivalence, len(normalized)
        return best

    def aliases_for_model_id(self, model_id: str) -> tuple[str, ...]:
        """The benchmark names ``model_id`` maps to (empty if unmatched)."""
        matched = self._match_model_id(model_id)
        return matched.aliases if matched is not None else ()

    def canonical_for_model_id(self, model_id: str) -> str | None:
        """The canonical key ``model_id`` maps to (``None`` if unmatched)."""
        matched = self._match_model_id(model_id)
        return matched.canonical if matched is not None else None

    def canonical_for_alias(self, alias: str) -> str | None:
        """The canonical key for an exact benchmark ``alias`` (``None`` if unknown)."""
        for equivalence in self.equivalences:
            if alias in equivalence.aliases:
                return equivalence.canonical
        return None

    def id_patterns_for_alias(self, alias: str) -> tuple[str, ...]:
        """The id patterns for an exact benchmark ``alias`` (empty if unknown)."""
        for equivalence in self.equivalences:
            if alias in equivalence.aliases:
                return equivalence.id_patterns
        return ()


def parse_equivalence_table(payload: dict) -> EquivalenceTable:
    """Validate a mapping payload into an :class:`EquivalenceTable`.

    Args:
        payload: The decoded JSON, ``{"equivalences": [...]}``.

    Returns:
        The validated table.

    Raises:
        pydantic.ValidationError: If the payload violates the contract.
    """
    return EquivalenceTable.model_validate(payload)


def load_equivalence_table(path: Path | None = None) -> EquivalenceTable:
    """Load and validate the versioned equivalence mapping.

    Args:
        path: The JSON resource to read; defaults to
            ``benchmark_equivalences.json`` beside this module.

    Returns:
        The validated table.

    Raises:
        json.JSONDecodeError: If the resource is not valid JSON.
        pydantic.ValidationError: If the resource violates the contract.
    """
    text = (path or _RESOURCE).read_text(encoding="utf-8")
    return parse_equivalence_table(json.loads(text))
