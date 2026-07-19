"""The model-reference value object.

A ``provider/model`` string (``"nvidia_nim/mistralai/mistral-medium-3.5-128b"``)
is the atom of routing configuration. Before this module it travelled the
system as a bare string parsed ad hoc; :class:`ModelRef` makes it a
validated value object that round-trips its ``chains.env`` spelling and
adapts to the legacy ``ResolvedModel`` boundary without the dispatch
layer ever seeing a raw string.

The provider id is validated against :data:`SUPPORTED_PROVIDER_IDS`,
which derives from the provider catalog — so registering a new provider
there makes :class:`ModelRef` accept it with no edit here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator

from repoach.llm_proxy.config.provider_ids import SUPPORTED_PROVIDER_IDS

if TYPE_CHECKING:
    from repoach.llm_proxy.api.model_router import ResolvedModel


class ModelRef(BaseModel):
    """A validated ``provider/model`` reference.

    Frozen value object: the provider must be a registered provider id,
    the model part is everything after the first ``/`` (so multi-segment
    model names survive intact). ``str(ref)`` round-trips the original
    ``chains.env`` spelling.
    """

    model_config = ConfigDict(frozen=True, protected_namespaces=())

    provider_id: str
    model: str

    @field_validator("provider_id")
    @classmethod
    def _known_provider(cls, value: str) -> str:
        """Reject a provider id that is not in the catalog."""
        if value not in SUPPORTED_PROVIDER_IDS:
            supported = ", ".join(SUPPORTED_PROVIDER_IDS)
            raise ValueError(f"unknown provider {value!r}; registered: {supported}")
        return value

    @field_validator("model")
    @classmethod
    def _reject_empty_model(cls, value: str) -> str:
        """Forbid an empty model segment (``"nvidia_nim/"``)."""
        if not value:
            raise ValueError("ModelRef.model must be non-empty")
        return value

    @classmethod
    def parse(cls, ref: str) -> ModelRef:
        """Parse a ``provider/model`` string into a :class:`ModelRef`.

        Splits on the FIRST ``/`` only, so the model segment keeps any
        further slashes (``"nvidia_nim/mistralai/x"`` →
        ``model="mistralai/x"``).

        Args:
            ref: The ``provider/model`` reference to parse.

        Returns:
            The parsed reference.

        Raises:
            ValueError: If ``ref`` has no ``/`` separator, an empty model
                segment, or an unregistered provider prefix.
        """
        provider, separator, model = ref.partition("/")
        if not separator:
            raise ValueError(f"model ref {ref!r} is missing a '/' separator")
        return cls(provider_id=provider, model=model)

    def __str__(self) -> str:
        """Round-trip the original ``provider/model`` spelling."""
        return f"{self.provider_id}/{self.model}"

    def to_resolved(self, original_model: str) -> ResolvedModel:
        """Adapt to the legacy dispatch boundary type.

        Reproduces :meth:`ModelRouter._build_resolved` so the failover
        loop keeps consuming an identical ``ResolvedModel``. The import is
        local to keep :mod:`repoach.llm_proxy.routing` free of any
        dependency on the ``api`` layer.

        Args:
            original_model: The client-supplied alias that resolved to
                this reference (carried through for logging/traceability).

        Returns:
            The equivalent ``ResolvedModel``.
        """
        from repoach.llm_proxy.api.model_router import ResolvedModel

        return ResolvedModel(
            original_model=original_model,
            provider_id=self.provider_id,
            provider_model=self.model,
            provider_model_ref=str(self),
        )
