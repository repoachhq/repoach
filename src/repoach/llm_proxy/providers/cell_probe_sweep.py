"""Probe-matrix sweep orchestrator (SP-CHAINPILOT-PROBE-SWEEP, Phase 2a-2).

Fans the per-cell probe (2a-1) across a whole ``(provider × model)`` matrix
(1b): resolve each provider's endpoint once via the canonical
:func:`build_provider_config` (reused, no second copy of the credential logic),
then probe every cell under a bounded concurrency, emitting exactly **one**
:class:`CellHealth` per matrix cell. A cell whose provider cannot be resolved
becomes an ``error`` health with no network call, so the result stays 1:1 with
the matrix and the persisted record (``cell_probe_store``) is complete.

The orchestrator never raises (``probe_cell`` never raises) and performs no I/O
of its own beyond the injected client. It is additive and unwired — no schedule
or CLI runs it yet (the cadence is 3e); it is a callable capability exactly as
1b's ``sweep_model_matrix`` is.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any

import httpx

from repoach.core.logging import get_logger
from repoach.health.model_health import STATUS_ERROR
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.cell_probe import CellHealth, probe_cell
from repoach.llm_proxy.providers.exceptions import AuthenticationError
from repoach.llm_proxy.providers.model_matrix import ProviderModelMatrix
from repoach.llm_proxy.providers.registry import (
    PROVIDER_DESCRIPTORS,
    ProviderDescriptor,
    build_provider_config,
)

_log = get_logger(__name__)

_PROBE_PROMPT = "Reply with the single word: ok"

__all__ = ["sweep_cell_health"]


def _resolve_endpoints(
    matrix: ProviderModelMatrix,
    settings: Settings,
    descriptors: Mapping[str, ProviderDescriptor],
) -> dict[str, tuple[str, str] | None]:
    """Resolve ``(base_url, api_key)`` once per provider present in the matrix.

    A provider missing from *descriptors* or without a credential
    (:class:`AuthenticationError`) maps to ``None`` — its cells then become
    no-credential error health rather than reaching the network. Logged once
    per unresolved provider.
    """
    endpoints: dict[str, tuple[str, str] | None] = {}
    for provider_id in matrix.providers():
        descriptor = descriptors.get(provider_id)
        if descriptor is None:
            _log.warning("cell_sweep_unknown_provider", provider=provider_id)
            endpoints[provider_id] = None
            continue
        try:
            config = build_provider_config(descriptor, settings)
        except AuthenticationError as exc:
            _log.warning("cell_sweep_no_credential", provider=provider_id, detail=str(exc))
            endpoints[provider_id] = None
            continue
        endpoints[provider_id] = (config.base_url or "", config.api_key)
    return endpoints


async def sweep_cell_health(
    matrix: ProviderModelMatrix,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    descriptors: Mapping[str, ProviderDescriptor] = PROVIDER_DESCRIPTORS,
    max_concurrency: int = 8,
    extra_body: Mapping[str, Any] | None = None,
    prompt: str = _PROBE_PROMPT,
    max_tokens: int = 64,
    timeout_s: float = 30.0,
    slow_threshold_s: float = 8.0,
    pacing_s: float = 0.0,
) -> list[CellHealth]:
    """Probe every cell of *matrix* under a concurrency bound. Never raises.

    Resolves each provider's endpoint once, then probes all ``matrix.cells``
    concurrently (at most *max_concurrency* in flight), returning one
    :class:`CellHealth` per cell in ``matrix.cells`` order.

    Args:
        matrix: The ``(provider × model)`` matrix to probe (1b).
        settings: Proxy settings, source of base-url overrides and keys.
        client: A caller-owned ``httpx.AsyncClient``.
        descriptors: The provider descriptor table (defaults to the full
            :data:`PROVIDER_DESCRIPTORS`; injectable for tests).
        max_concurrency: Maximum simultaneous ``probe_cell`` calls.
        extra_body: Optional fields merged into every probe body (reasoning
            knobs are 2a-3's job; default ``None`` = a plain health sweep).
        prompt: The probe user message.
        max_tokens: Per-probe completion budget.
        timeout_s: Per-probe hard cap.
        slow_threshold_s: Latency above which a probe is reported ``slow``.
        pacing_s: Optional inter-probe delay in seconds; when > 0 an
            ``asyncio.sleep(pacing_s)`` is inserted after the semaphore
            acquisition and before the ``probe_cell`` call. Default 0.0
            (no pacing).

    Returns:
        One :class:`CellHealth` per cell, in ``matrix.cells`` order.
    """
    endpoints = _resolve_endpoints(matrix, settings, descriptors)
    semaphore = asyncio.Semaphore(max_concurrency)

    async def _probe(cell) -> CellHealth:
        endpoint = endpoints.get(cell.provider_id)
        if endpoint is None:
            return CellHealth(
                cell.provider_id, cell.model_id, STATUS_ERROR, None, 0, 0, "no credential"
            )
        base_url, api_key = endpoint
        async with semaphore:
            if pacing_s > 0:
                await asyncio.sleep(pacing_s)
            return await probe_cell(
                client,
                provider_id=cell.provider_id,
                base_url=base_url,
                api_key=api_key,
                model=cell.model_id,
                prompt=prompt,
                max_tokens=max_tokens,
                timeout_s=timeout_s,
                slow_threshold_s=slow_threshold_s,
                extra_body=extra_body,
            )

    return list(await asyncio.gather(*(_probe(cell) for cell in matrix.cells)))
