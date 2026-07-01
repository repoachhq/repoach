"""The (provider × model) matrix — domain type + sweep builder (SP-CHAINPILOT-MATRIX).

Phase 1b of the Chain Autopilot arc. Turns the per-provider lister from 1a into
the observatory's structural core: the live ``(provider × model)`` matrix.

The value objects (:class:`ModelCell`, :class:`ProviderModelMatrix`) and the
:func:`assemble_matrix` flattener are pure and I/O-free; only
:func:`sweep_model_matrix` touches ``Settings`` and the network (with a
caller-injected client). The sweep reuses the canonical
:func:`build_provider_config` to resolve each provider's endpoint — avoiding a
second copy of the credential/base-url logic — and skips a provider whose
credential is missing (``AuthenticationError``) gracefully rather than aborting.
``claude_code`` is excluded up front via :func:`is_sweepable` (subprocess
backstop, no ``/v1/models``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import httpx

from ferova.core.logging import get_logger
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.providers.exceptions import AuthenticationError
from ferova.llm_proxy.providers.model_catalog import (
    ProviderModelListing,
    is_sweepable,
    list_provider_models,
)
from ferova.llm_proxy.providers.registry import (
    PROVIDER_DESCRIPTORS,
    ProviderDescriptor,
    build_provider_config,
)

_log = get_logger(__name__)

__all__ = [
    "ModelCell",
    "ProviderModelMatrix",
    "assemble_matrix",
    "sweep_model_matrix",
]


@dataclass(frozen=True, slots=True)
class ModelCell:
    """One ``(provider, model)`` cell — the arc's unit of evaluation."""

    provider_id: str
    model_id: str


@dataclass(frozen=True, slots=True)
class ProviderModelMatrix:
    """The live ``(provider × model)`` matrix from one catalog sweep.

    Attributes:
        cells: The flattened successful pairs, one per model of each ``ok``
            listing.
        listings: The per-provider provenance, preserved verbatim — including
            providers that failed or were skipped (``ok=False``, no cells).
    """

    cells: tuple[ModelCell, ...]
    listings: tuple[ProviderModelListing, ...]

    def __len__(self) -> int:
        """The number of cells in the matrix."""
        return len(self.cells)

    def providers(self) -> tuple[str, ...]:
        """The provider ids that contributed at least one cell, in order."""
        seen: dict[str, None] = {}
        for cell in self.cells:
            seen.setdefault(cell.provider_id, None)
        return tuple(seen)

    def models_for(self, provider_id: str) -> tuple[str, ...]:
        """The model ids the matrix holds for ``provider_id`` (empty if none)."""
        return tuple(cell.model_id for cell in self.cells if cell.provider_id == provider_id)


def assemble_matrix(listings: Sequence[ProviderModelListing]) -> ProviderModelMatrix:
    """Flatten per-provider listings into a :class:`ProviderModelMatrix`.

    Cells are produced only from ``ok`` listings (a failed listing carries no
    models); every listing is preserved in ``listings`` for provenance.

    Args:
        listings: The per-provider outcomes from the sweep.

    Returns:
        The assembled matrix.
    """
    cells = tuple(
        ModelCell(listing.provider_id, model.model_id)
        for listing in listings
        if listing.ok
        for model in listing.models
    )
    return ProviderModelMatrix(cells=cells, listings=tuple(listings))


def _resolve_endpoint(descriptor: ProviderDescriptor, settings: Settings) -> tuple[str, str] | None:
    """Resolve a descriptor to ``(base_url, api_key)`` or ``None`` if unkeyed.

    Reuses the canonical :func:`build_provider_config`; a missing credential
    surfaces as :class:`AuthenticationError`, which is caught and reported as a
    skip (``None``) rather than aborting the sweep.
    """
    try:
        config = build_provider_config(descriptor, settings)
    except AuthenticationError as exc:
        _log.warning("matrix_sweep_no_credential", provider=descriptor.provider_id, detail=str(exc))
        return None
    return config.base_url or "", config.api_key


async def sweep_model_matrix(
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    descriptors: Mapping[str, ProviderDescriptor] = PROVIDER_DESCRIPTORS,
) -> ProviderModelMatrix:
    """Sweep every sweepable provider's ``/v1/models`` into a matrix. Never raises.

    Iterates ``descriptors``, skips the unsweepable backstops, resolves each
    endpoint, lists its models (1a), and assembles the result. A provider
    without a credential is recorded as a ``"no credential"`` failure listing
    and never hits the network; an unkeyed or unreachable provider therefore
    degrades the matrix without breaking it.

    Args:
        settings: Proxy settings, the source of base-url overrides and keys.
        client: A caller-owned ``httpx.AsyncClient``.
        descriptors: The provider descriptor table to sweep (defaults to the
            full :data:`PROVIDER_DESCRIPTORS`; injectable for tests).

    Returns:
        The assembled :class:`ProviderModelMatrix`.
    """
    listings: list[ProviderModelListing] = []
    for provider_id, descriptor in descriptors.items():
        if not is_sweepable(provider_id):
            continue
        endpoint = _resolve_endpoint(descriptor, settings)
        if endpoint is None:
            listings.append(ProviderModelListing(provider_id, (), False, "no credential"))
            continue
        base_url, api_key = endpoint
        listings.append(
            await list_provider_models(
                client, provider_id=provider_id, base_url=base_url, api_key=api_key
            )
        )
    return assemble_matrix(listings)
