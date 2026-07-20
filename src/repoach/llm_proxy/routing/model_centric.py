"""Model-centric chain structure (SP-CHAINPILOT-CHAIN-MODEL-CENTRIC).

Phase 1e of the Chain Autopilot arc — the last Phase-1 brick. The model-centric
view of a chain (Principle 2): a chain seen as an ordered list of *models* (by
quality), each fanning out to all of its providers, so failover has two axes —
across models and, within a model, across providers.

It lands additively, **behind** the existing flat :class:`Chain`: the flat type
and all routing/dispatch behaviour are untouched. This is a derivable lens,
wired by nobody yet. :meth:`ModelCentricChain.from_chain` groups a flat chain's
refs by their model segment; an optional ``key`` callable lets the *semantic*
grouping (the same model under different provider IDs) be layered later via the
1d equivalence resolver, without reshaping this type — that composition lives in
2d, so 1e itself imports nothing owned.
"""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict, field_validator

from repoach.llm_proxy.routing.chain import Chain
from repoach.llm_proxy.routing.refs import ModelRef

__all__ = [
    "ModelCentricChain",
    "ModelGroup",
]


class ModelGroup(BaseModel):
    """One model and the ordered providers that serve it.

    Attributes:
        model: The grouping identity — a ref's model segment by default, or a
            canonical id when :meth:`ModelCentricChain.from_chain` is given a
            custom ``key``.
        providers: The :class:`ModelRef` entries for this model, in
            first-occurrence order; never empty.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    model: str
    providers: tuple[ModelRef, ...]

    @field_validator("providers")
    @classmethod
    def _reject_empty(cls, value: tuple[ModelRef, ...]) -> tuple[ModelRef, ...]:
        """Forbid a model group with no providers."""
        if not value:
            raise ValueError("ModelGroup must contain at least one provider")
        return value

    def head(self) -> ModelRef:
        """The first (preferred) provider for this model."""
        return self.providers[0]


class ModelCentricChain(BaseModel):
    """An ordered list of :class:`ModelGroup` — the model-centric chain view."""

    model_config = ConfigDict(frozen=True)

    groups: tuple[ModelGroup, ...]

    @field_validator("groups")
    @classmethod
    def _reject_empty(cls, value: tuple[ModelGroup, ...]) -> tuple[ModelGroup, ...]:
        """Forbid an empty model-centric chain — it always has a head model."""
        if not value:
            raise ValueError("ModelCentricChain must contain at least one ModelGroup")
        return value

    @classmethod
    def from_chain(
        cls, chain: Chain, *, key: Callable[[ModelRef], str] | None = None
    ) -> ModelCentricChain:
        """Derive the model-centric view of a flat :class:`Chain`.

        Groups the chain's refs by ``key`` (default: the ref's ``model``
        segment), preserving the first-occurrence order of both the groups and
        the providers within each.

        Args:
            chain: The flat failover chain to regroup.
            key: How to identify a model from a ref; defaults to ``ref.model``.
                Pass an equivalence-based key (2d) for semantic cross-provider
                grouping.

        Returns:
            The grouped model-centric chain.
        """
        extract = key if key is not None else (lambda ref: ref.model)
        order: list[str] = []
        buckets: dict[str, list[ModelRef]] = {}
        for ref in chain.refs:
            identity = extract(ref)
            if identity not in buckets:
                buckets[identity] = []
                order.append(identity)
            buckets[identity].append(ref)
        groups = tuple(
            ModelGroup(model=identity, providers=tuple(buckets[identity])) for identity in order
        )
        return cls(groups=groups)

    def to_chain(self) -> Chain:
        """Flatten back to a flat :class:`Chain` in group-major order.

        Providers of each model are emitted adjacently; a chain whose providers
        were already model-adjacent round-trips exactly, an interleaved one
        yields the regrouped (model-centric) order.
        """
        refs = tuple(ref for group in self.groups for ref in group.providers)
        return Chain(refs=refs)

    def models(self) -> tuple[str, ...]:
        """The model identities, in order."""
        return tuple(group.model for group in self.groups)

    def providers_for(self, model: str) -> tuple[ModelRef, ...]:
        """The providers serving ``model`` (empty if absent)."""
        for group in self.groups:
            if group.model == model:
                return group.providers
        return ()

    def __len__(self) -> int:
        """The number of model groups."""
        return len(self.groups)
