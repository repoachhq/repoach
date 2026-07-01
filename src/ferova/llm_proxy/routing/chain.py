"""An ordered failover chain of model references.

A ``MODEL_*`` slot is either a single ``provider/model`` ref or a
comma-separated chain (primary first, fallbacks after). :class:`Chain`
is that chain as a value object: ordered, de-duplicated, never empty.
De-duplication is a deliberate hardening over the legacy comma-split
(``Settings._split_chain``), which let a slot list the same ref twice;
the current ``chains.env`` has no duplicates, so behaviour is unchanged
in practice.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, field_validator

from ferova.llm_proxy.routing.refs import ModelRef


class Chain(BaseModel):
    """An ordered, de-duplicated, non-empty list of :class:`ModelRef`."""

    model_config = ConfigDict(frozen=True)

    refs: tuple[ModelRef, ...]

    @field_validator("refs")
    @classmethod
    def _reject_empty(cls, value: tuple[ModelRef, ...]) -> tuple[ModelRef, ...]:
        """Forbid an empty chain — every chain has at least a head."""
        if not value:
            raise ValueError("Chain must contain at least one ModelRef")
        return value

    @classmethod
    def parse(cls, spec: str) -> Chain:
        """Parse a comma-separated ``MODEL_*`` slot value into a chain.

        Reproduces ``Settings._split_chain`` (strip each part, drop
        empties) then parses each part with :meth:`ModelRef.parse`,
        de-duplicating while preserving order (first occurrence wins).

        Args:
            spec: The slot value, e.g.
                ``"nvidia_nim/a/b,claude_code/opus"``.

        Returns:
            The parsed chain.

        Raises:
            ValueError: If ``spec`` contains no non-empty entries, or any
                entry is not a valid ``provider/model`` reference.
        """
        seen: set[str] = set()
        refs: list[ModelRef] = []
        for part in spec.split(","):
            stripped = part.strip()
            if not stripped:
                continue
            ref = ModelRef.parse(stripped)
            key = str(ref)
            if key in seen:
                continue
            seen.add(key)
            refs.append(ref)
        if not refs:
            raise ValueError(f"chain spec {spec!r} has no entries")
        return cls(refs=tuple(refs))

    def without(self, skip: frozenset[ModelRef]) -> Chain:
        """Return this chain with ``skip`` removed, never empty.

        Reproduces the semantic-failover filter at
        ``model_router.py:85`` (``[...] or [chain[0]]``): if every ref is
        skipped, falls back to a single-entry chain of the original head
        so the caller surfaces the final failure rather than looping on
        an empty chain.

        Args:
            skip: References the caller has already tried and rejected.

        Returns:
            The filtered chain, or the original head if all were skipped.
        """
        filtered = tuple(ref for ref in self.refs if ref not in skip)
        if not filtered:
            return Chain(refs=(self.refs[0],))
        return Chain(refs=filtered)
