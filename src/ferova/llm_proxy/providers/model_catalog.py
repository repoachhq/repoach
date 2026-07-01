"""Per-provider model listing — the catalog sweep's eyes (SP-CHAINPILOT-CATALOG-MODELS).

Phase 1a of the Chain Autopilot arc. Asks one provider "which models do you
serve?" by hitting its upstream ``GET {base_url}/models`` and returning the
parsed ids. This is the lowest brick of the observatory: pure id discovery,
deliberately blind to capability tiers (the per-category selection happens
downstream in the benchmark/equivalence/decision slices).

The function mirrors :func:`ferova.review.chain_health.probe_nim_model`:
the caller owns and injects the ``httpx.AsyncClient`` plus the already-resolved
``base_url`` / ``api_key`` (turning a :data:`PROVIDER_DESCRIPTORS` entry into
those is the matrix builder's job, 1b), so this leaf stays decoupled from
``Settings`` and trivially testable with a fake transport. It never raises: a
dead or misbehaving provider is captured as ``ok=False`` with a ``detail``
string and logged, so one bad provider never aborts a full sweep.

``claude_code`` is excluded from the sweep — it is a subprocess backstop with
no HTTP ``/v1/models`` endpoint, so :func:`is_sweepable` rejects it.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ferova.core.logging import get_logger

_log = get_logger(__name__)

UNSWEPT_PROVIDERS: frozenset[str] = frozenset({"claude_code"})

__all__ = [
    "UNSWEPT_PROVIDERS",
    "ListedModel",
    "ProviderModelListing",
    "is_sweepable",
    "list_provider_models",
]


@dataclass(frozen=True, slots=True)
class ListedModel:
    """One model id advertised by a provider's ``/v1/models`` endpoint."""

    model_id: str


@dataclass(frozen=True, slots=True)
class ProviderModelListing:
    """Outcome of listing one provider's models.

    Attributes:
        provider_id: The provider that was queried.
        models: The parsed model ids; always a tuple, empty on failure.
        ok: ``True`` on a 2xx response with a well-formed body.
        detail: A short human-readable note — the model count on success,
            or the failure reason (status code, shape problem, transport
            error) otherwise.
    """

    provider_id: str
    models: tuple[ListedModel, ...]
    ok: bool
    detail: str


def is_sweepable(provider_id: str) -> bool:
    """Whether a provider's models can be discovered via ``/v1/models``.

    Args:
        provider_id: The stable provider identifier.

    Returns:
        ``False`` for the subprocess backstops in :data:`UNSWEPT_PROVIDERS`
        (currently ``claude_code``), ``True`` otherwise.
    """
    return provider_id not in UNSWEPT_PROVIDERS


def _redact(text: str, secret: str) -> str:
    """Mask ``secret`` in ``text`` so the api key can never reach a log sink."""
    return text.replace(secret, "***") if secret else text


def _parse_models(payload: object) -> tuple[tuple[ListedModel, ...], str | None]:
    """Extract model ids from an OpenAI-shape ``/v1/models`` body.

    Args:
        payload: The decoded JSON body, expected as
            ``{"data": [{"id": str, ...}, ...]}``.

    Returns:
        ``(models, shape_error)``. On a valid shape ``shape_error`` is
        ``None`` and ``models`` holds one :class:`ListedModel` per entry
        carrying a non-empty string ``id`` (malformed entries are skipped).
        On an unexpected shape ``models`` is empty and ``shape_error``
        names the problem.
    """
    if not isinstance(payload, dict):
        return (), f"unexpected body type: {type(payload).__name__}"
    data = payload.get("data")
    if not isinstance(data, list):
        return (), "body missing a 'data' list"
    models: list[ListedModel] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        model_id = entry.get("id")
        if isinstance(model_id, str) and model_id:
            models.append(ListedModel(model_id))
    return tuple(models), None


async def list_provider_models(
    client: httpx.AsyncClient,
    *,
    provider_id: str,
    base_url: str,
    api_key: str,
    timeout_s: float = 15.0,
) -> ProviderModelListing:
    """List one provider's models via ``GET {base_url}/models``. Never raises.

    Issues a bearer-authenticated GET against the provider's catalog endpoint
    and parses the OpenAI-shape body. Any failure — non-2xx status, malformed
    body, transport error — is captured as ``ok=False`` with a populated
    ``detail`` and logged at warning level (caught-and-logged, never silenced),
    so a single dead provider never aborts the sweep.

    Args:
        client: A caller-owned ``httpx.AsyncClient``.
        provider_id: The provider being queried (for the result and logs).
        base_url: The provider base URL, already including the ``/v1``
            segment; the endpoint is ``{base_url}/models``.
        api_key: The bearer credential; may be empty for keyless locals.
        timeout_s: Per-request timeout in seconds.

    Returns:
        A :class:`ProviderModelListing` describing the outcome.
    """
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    try:
        resp = await client.get(url, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        detail = _redact(f"{type(exc).__name__}: {str(exc)[:120]}", api_key)
        _log.warning("catalog_list_transport_failed", provider=provider_id, detail=detail)
        return ProviderModelListing(provider_id, (), False, detail)
    if not 200 <= resp.status_code < 300:
        detail = f"http={resp.status_code}"
        _log.warning("catalog_list_http_error", provider=provider_id, detail=detail)
        return ProviderModelListing(provider_id, (), False, detail)
    try:
        payload = resp.json()
    except ValueError as exc:
        detail = f"json_decode: {type(exc).__name__}"
        _log.warning("catalog_list_unparseable", provider=provider_id, detail=detail)
        return ProviderModelListing(provider_id, (), False, detail)
    models, shape_error = _parse_models(payload)
    if shape_error is not None:
        _log.warning("catalog_list_bad_shape", provider=provider_id, detail=shape_error)
        return ProviderModelListing(provider_id, (), False, shape_error)
    detail = f"{len(models)} models"
    _log.info("catalog_listed", provider=provider_id, count=len(models))
    return ProviderModelListing(provider_id, models, True, detail)
