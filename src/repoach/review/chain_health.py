"""NIM chain head-health probe (SP-NIM-CHAIN-HEALTH).

Probes each capability tier's head model directly against NIM and
classifies its health, so a drifting head — the HTTP-200-but-empty
thinking-leak (qwen3.5-122b, 2026-06-09) or a latency spike — is
surfaced even though SP-PROXY-THINKING-BUDGET-RETRY now lets the chain
self-heal and would otherwise hide it. The probe deliberately bypasses
the proxy (budget-retry + failover) to measure the raw NIM head.
"""

from __future__ import annotations

import asyncio
import time

import httpx

from repoach.core.logging import get_logger
from repoach.health.model_health import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_SLOW,
    ModelHealth,
    is_degraded,
)
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.defaults import NVIDIA_NIM_BASE_URL
from repoach.llm_proxy.providers.model_catalog import redact_secret

_log = get_logger(__name__)

CHAIN_TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku")
_PROBE_PROMPT = "Reply with the single word: ok"

__all__ = [
    "STATUS_EMPTY",
    "STATUS_ERROR",
    "STATUS_OK",
    "STATUS_SKIPPED",
    "STATUS_SLOW",
    "ModelHealth",
    "chain_head",
    "check_tier_heads",
    "classify",
    "is_degraded",
]


def chain_head(chain_value: str) -> tuple[str, str]:
    """Split the head ``provider/model`` of a comma-separated chain.

    Args:
        chain_value: A ``MODEL_*`` value such as
            ``"nvidia_nim/qwen/qwen3.5-122b-a10b,claude_code/sonnet"``.

    Returns:
        ``(provider, model)`` of the first entry; ``model`` keeps any
        nested slashes (``"qwen/qwen3.5-122b-a10b"``).
    """
    head = chain_value.split(",")[0].strip()
    provider, _, model = head.partition("/")
    return provider, model


def classify(
    status_code: int | None,
    latency_s: float | None,
    content: str,
    *,
    slow_threshold_s: float,
) -> str:
    """Classify a probe outcome into a health status.

    Args:
        status_code: HTTP status, or ``None`` for a transport failure.
        latency_s: Probe wall-clock seconds, or ``None``.
        content: The returned assistant text.
        slow_threshold_s: Latency above which a successful probe is
            reported ``slow`` rather than ``ok``.

    Returns:
        ``error`` (non-2xx / no response), ``empty`` (2xx but blank
        text — the thinking-leak signature), ``slow`` (content but over
        threshold), or ``ok``.
    """
    if status_code is None or not (200 <= status_code < 300):
        return STATUS_ERROR
    if not content or not content.strip():
        return STATUS_EMPTY
    if latency_s is not None and latency_s > slow_threshold_s:
        return STATUS_SLOW
    return STATUS_OK


def _extract_content(payload: object) -> str:
    """Return ``choices[0].message.content`` from a NIM JSON payload."""
    if not isinstance(payload, dict):
        return ""
    choices = payload.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    return content if isinstance(content, str) else ""


async def probe_nim_model(
    client: httpx.AsyncClient,
    base_url: str,
    api_key: str,
    model: str,
    *,
    tier: str,
    prompt: str = _PROBE_PROMPT,
    max_tokens: int = 16,
    timeout_s: float = 30.0,
    slow_threshold_s: float = 8.0,
) -> ModelHealth:
    """Probe one NIM model once and classify it. Never raises.

    A transport error or timeout is captured as ``status="error"`` so a
    single dead model never aborts a multi-tier sweep.
    """
    url = f"{base_url.rstrip('/')}/chat/completions"
    body = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"Authorization": f"Bearer {api_key}", "content-type": "application/json"}
    start = time.monotonic()
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=timeout_s)
    except httpx.HTTPError as exc:
        detail = redact_secret(f"{type(exc).__name__}: {exc}", api_key)
        _log.warning("nim_chain_probe_transport_failed", tier=tier, model=model, detail=detail)
        return ModelHealth(tier, model, STATUS_ERROR, None, 0, detail)
    latency_s = time.monotonic() - start
    try:
        content = _extract_content(resp.json())
    except ValueError as exc:
        body_snippet = redact_secret(resp.text, api_key, limit=200)
        detail = (
            f"json_decode: {type(exc).__name__} status={resp.status_code} body={body_snippet!r}"
        )
        _log.warning(
            "nim_chain_probe_unparseable",
            tier=tier,
            model=model,
            status_code=resp.status_code,
            body_snippet=body_snippet,
            detail=detail,
        )
        return ModelHealth(tier, model, STATUS_ERROR, latency_s, 0, detail)
    status = classify(resp.status_code, latency_s, content, slow_threshold_s=slow_threshold_s)
    detail = content.strip()[:40] if content.strip() else f"http={resp.status_code}"
    _log.info(
        "nim_chain_health",
        tier=tier,
        model=model,
        status=status,
        latency_s=round(latency_s, 2),
        content_chars=len(content),
    )
    return ModelHealth(tier, model, status, latency_s, len(content), detail)


async def _resolve_tier_head(
    tier: str,
    chain_value: str | None,
    settings: Settings,
    client: httpx.AsyncClient,
    *,
    slow_threshold_s: float,
    max_tokens: int,
    timeout_s: float,
) -> ModelHealth:
    """Resolve one tier: probe its NIM head, or report it skipped."""
    if not chain_value:
        return ModelHealth(tier, "", STATUS_SKIPPED, None, 0, "no chain configured")
    provider, model = chain_head(chain_value)
    if provider != "nvidia_nim":
        return ModelHealth(tier, f"{provider}/{model}", STATUS_SKIPPED, None, 0, "non-NIM head")
    return await probe_nim_model(
        client,
        NVIDIA_NIM_BASE_URL,
        settings.nvidia_nim_api_key,
        model,
        tier=tier,
        max_tokens=max_tokens,
        timeout_s=timeout_s,
        slow_threshold_s=slow_threshold_s,
    )


async def check_tier_heads(
    settings: Settings,
    *,
    client: httpx.AsyncClient,
    slow_threshold_s: float = 8.0,
    max_tokens: int = 16,
    timeout_s: float = 30.0,
) -> list[ModelHealth]:
    """Probe every tier's head model concurrently; non-NIM heads skipped.

    The per-tier probes run under :func:`asyncio.gather`, so a fully
    degraded NIM costs one ``timeout_s`` window rather than the sum of
    four; the returned list still preserves ``CHAIN_TIERS`` order.

    Args:
        settings: Proxy settings carrying the per-tier chains and the
            NIM api key.
        client: An ``httpx.AsyncClient`` (injected so tests never touch
            the network).
        slow_threshold_s: Forwarded to :func:`classify`.
        max_tokens: Probe completion budget.
        timeout_s: Per-probe hard cap.

    Returns:
        One :class:`ModelHealth` per tier, in ``CHAIN_TIERS`` order.
    """
    chains: dict[str, str | None] = {
        "opus": settings.model_opus,
        "sonnet": settings.model_sonnet,
        "haiku": settings.model_haiku,
    }
    probes = [
        _resolve_tier_head(
            tier,
            chains[tier],
            settings,
            client,
            slow_threshold_s=slow_threshold_s,
            max_tokens=max_tokens,
            timeout_s=timeout_s,
        )
        for tier in CHAIN_TIERS
    ]
    return list(await asyncio.gather(*probes))
