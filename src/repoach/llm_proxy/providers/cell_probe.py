"""Per-cell probe primitive (SP-CHAINPILOT-PROBE-CELL, Phase 2a-1).

The provider-agnostic probe of one ``(provider, model)`` cell of the matrix:
a single chat completion against the cell's endpoint, classified into the
neutral health vocabulary plus a reasoning observation. It generalises the
NIM-only ``review.chain_health.probe_nim_model`` to every OpenAI-compatible
provider and adds awareness of the reasoning dimension the chains now must
handle (Principle 4): it records how much hidden reasoning a cell emitted and
whether it still returned a visible answer or starved it.

This leaf only *observes* — it injects no reasoning knob of its own. Whether to
enable reasoning, and which effort to request, is the effort probe's job
(2a-3); this primitive faithfully reports ``reasoning_chars`` for whatever
``extra_body`` it is handed. Endpoint and credential resolution stay the
sweep's job (2a-2): the caller injects an ``httpx.AsyncClient`` and an
already-resolved ``(base_url, api_key)``, so the probe is Settings-free and
decoupled, exactly as the 1a catalog lister is.

It reuses the neutral ``STATUS_*`` constants from
:mod:`repoach.health.model_health` so its results speak the same language as
the existing health store, without importing the NIM-specific
``review.chain_health`` (which would invert the dependency direction).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from repoach.core.logging import get_logger
from repoach.health.model_health import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SLOW,
)
from repoach.llm_proxy.providers.model_catalog import redact_secret

_log = get_logger(__name__)

_PROBE_PROMPT = "Reply with the single word: ok"
_REASONING_KEYS: tuple[str, ...] = ("reasoning_content", "reasoning")

__all__ = [
    "CellHealth",
    "classify_cell",
    "probe_cell",
]


@dataclass(frozen=True, slots=True)
class CellHealth:
    """Health of one ``(provider, model)`` cell after a single probe.

    Attributes:
        provider_id: The catalog provider id the cell was probed on.
        model_id: The probed model id.
        status: One of ``ok`` / ``slow`` / ``empty`` / ``error`` (classified
            over the *visible* content only).
        latency_s: Wall-clock seconds for the probe, or ``None`` when no
            request completed (transport error).
        content_chars: Length of the visible assistant text.
        reasoning_chars: Length of the hidden reasoning text, or ``0`` when the
            cell emitted none.
        detail: A short human note — the visible-content preview on success,
            the error class or ``http=<code>`` on failure.
    """

    provider_id: str
    model_id: str
    status: str
    latency_s: float | None
    content_chars: int
    reasoning_chars: int
    detail: str

    @property
    def thinking_observed(self) -> bool:
        """Whether the cell emitted any hidden reasoning at all."""
        return self.reasoning_chars > 0

    @property
    def thinking_handled(self) -> bool:
        """Whether the cell reasoned *and* still returned a visible answer."""
        return self.reasoning_chars > 0 and self.content_chars > 0

    @property
    def thinking_starved(self) -> bool:
        """Whether the cell reasoned but starved its visible answer to nothing.

        The budget-starve signature Phase 0 set out to handle: hidden reasoning
        consumed the whole completion budget, leaving no visible output.
        """
        return self.reasoning_chars > 0 and self.content_chars == 0


def classify_cell(
    status_code: int | None,
    latency_s: float | None,
    content: str,
    *,
    slow_threshold_s: float,
) -> str:
    """Classify a probe outcome over its visible content.

    Reasoning is not visible output, so it never counts here — a cell that
    reasons but returns no visible text classifies ``empty`` (its
    :attr:`CellHealth.thinking_starved` flag then tells the starve story).

    Args:
        status_code: HTTP status, or ``None`` for a transport failure.
        latency_s: Probe wall-clock seconds, or ``None``.
        content: The returned *visible* assistant text.
        slow_threshold_s: Latency above which a successful probe is reported
            ``slow`` rather than ``ok``.

    Returns:
        ``error`` (non-2xx / no response), ``empty`` (2xx but blank visible
        text), ``slow`` (visible content but over threshold), or ``ok``.
    """
    if status_code is None or not (200 <= status_code < 300):
        return STATUS_ERROR
    if not content or not content.strip():
        return STATUS_EMPTY
    if latency_s is not None and latency_s > slow_threshold_s:
        return STATUS_SLOW
    return STATUS_OK


def _extract_message(payload: object) -> Mapping[str, Any]:
    """Return ``choices[0].message`` from an OpenAI-compatible payload."""
    if not isinstance(payload, dict):
        return {}
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return {}
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    return message if isinstance(message, dict) else {}


def _extract_content(message: Mapping[str, Any]) -> str:
    """Return the visible ``content`` string of an assistant message."""
    content = message.get("content")
    return content if isinstance(content, str) else ""


def _extract_reasoning(message: Mapping[str, Any]) -> str:
    """Return the hidden reasoning text, checking each known provider key.

    NIM-family providers carry it under ``reasoning_content``; the OpenRouter
    family under ``reasoning``. The first non-empty string wins.
    """
    for key in _REASONING_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


async def probe_cell(
    client: httpx.AsyncClient,
    *,
    provider_id: str,
    base_url: str,
    api_key: str,
    model: str,
    prompt: str = _PROBE_PROMPT,
    max_tokens: int = 64,
    timeout_s: float = 30.0,
    slow_threshold_s: float = 8.0,
    extra_body: Mapping[str, Any] | None = None,
) -> CellHealth:
    """Probe one ``(provider, model)`` cell once and classify it. Never raises.

    A transport error, timeout, non-2xx, or unparseable body is captured as
    ``status="error"`` so a single dead cell never aborts a matrix sweep.

    Args:
        client: An injected ``httpx.AsyncClient`` (tests never touch the
            network).
        provider_id: The catalog provider id (a label; it does not shape the
            request body).
        base_url: The provider's API root (already ending at ``/v1`` or the
            equivalent); ``/chat/completions`` is appended.
        api_key: The provider's bearer credential.
        model: The model id to probe.
        prompt: The user message to send.
        max_tokens: The completion budget for the probe.
        timeout_s: Per-probe hard cap.
        slow_threshold_s: Forwarded to :func:`classify_cell`.
        extra_body: Optional fields merged into the request body — the
            reasoning knobs the effort probe (2a-3) injects. ``None`` yields a
            plain probe.

    Returns:
        A :class:`CellHealth` describing the cell's status, latency, visible
        content size, and reasoning size.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    if extra_body:
        body.update(extra_body)
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    start = time.monotonic()
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        detail = redact_secret(f"{type(exc).__name__}: {exc}", api_key)
        _log.warning(
            "cell_probe_transport_failed", provider=provider_id, model=model, detail=detail
        )
        return CellHealth(provider_id, model, STATUS_ERROR, None, 0, 0, detail)
    latency_s = time.monotonic() - start
    if not (200 <= resp.status_code < 300):
        detail = f"http={resp.status_code}"
        _log.warning("cell_probe_http_error", provider=provider_id, model=model, detail=detail)
        return CellHealth(provider_id, model, STATUS_ERROR, latency_s, 0, 0, detail)
    try:
        message = _extract_message(resp.json())
    except ValueError as exc:
        detail = f"json_decode: {type(exc).__name__}"
        _log.warning("cell_probe_unparseable", provider=provider_id, model=model, detail=detail)
        return CellHealth(provider_id, model, STATUS_ERROR, latency_s, 0, 0, detail)
    content = _extract_content(message)
    reasoning = _extract_reasoning(message)
    status = classify_cell(resp.status_code, latency_s, content, slow_threshold_s=slow_threshold_s)
    detail = content.strip()[:40] if content.strip() else f"http={resp.status_code}"
    _log.info(
        "cell_probe_health",
        provider=provider_id,
        model=model,
        status=status,
        latency_s=round(latency_s, 2),
        content_chars=len(content),
        reasoning_chars=len(reasoning),
    )
    return CellHealth(provider_id, model, status, latency_s, len(content), len(reasoning), detail)
