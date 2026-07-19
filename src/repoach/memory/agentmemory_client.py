"""Thin REST client for the local ``agentmemory`` service (SP-BUILDER-MEMORY).

Wraps the running server's ``/agentmemory/{search,remember}`` endpoints.
Every call **degrades gracefully** — a down, slow, or malformed service
returns an empty / false result plus a warning, NEVER an exception — so
the builder is never blocked by its memory layer.
"""

from __future__ import annotations

import httpx

from ..core.logging import get_logger

_log = get_logger(__name__)


def recall(
    query: str,
    *,
    project: str,
    base_url: str,
    limit: int = 5,
    timeout_s: float = 4.0,
) -> list[str]:
    """Return up to *limit* memory narratives for *query* in *project*.

    Args:
        query: Free-text search query.
        project: The agentmemory project scope to search within.
        base_url: The service base URL (e.g. ``http://localhost:3111``).
        limit: Maximum hits to return.
        timeout_s: Per-request hard cap.

    Returns:
        Each hit's ``observation.narrative`` (fallback ``title``),
        newest/most-relevant first. ``[]`` on any failure — never raises.
    """
    url = f"{base_url.rstrip('/')}/agentmemory/search"
    try:
        resp = httpx.post(
            url, json={"query": query, "project": project, "limit": limit}, timeout=timeout_s
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        _log.warning(
            "agentmemory_recall_failed",
            project=project,
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return []
    out: list[str] = []
    results = payload.get("results") if isinstance(payload, dict) else None
    for hit in results or []:
        observation = hit.get("observation") if isinstance(hit, dict) else None
        if not isinstance(observation, dict):
            continue
        text = observation.get("narrative") or observation.get("title") or ""
        if isinstance(text, str) and text.strip():
            out.append(text.strip())
    return out


def remember(
    content: str,
    *,
    project: str,
    base_url: str,
    mem_type: str = "lesson",
    timeout_s: float = 4.0,
) -> bool:
    """Persist *content* as a memory in *project*.

    Args:
        content: The memory text.
        project: The agentmemory project scope to write into.
        base_url: The service base URL.
        mem_type: The agentmemory ``type`` field (e.g. ``"lesson"``).
        timeout_s: Per-request hard cap.

    Returns:
        ``True`` when the service reported success, else ``False`` —
        never raises.
    """
    url = f"{base_url.rstrip('/')}/agentmemory/remember"
    try:
        resp = httpx.post(
            url,
            json={"content": content, "project": project, "type": mem_type},
            timeout=timeout_s,
        )
        resp.raise_for_status()
        payload = resp.json()
    except Exception as exc:
        _log.warning(
            "agentmemory_remember_failed",
            project=project,
            error=f"{type(exc).__name__}: {str(exc)[:120]}",
        )
        return False
    return bool(isinstance(payload, dict) and payload.get("success"))
