"""Chain status digest — operator-visible chain health surface (SP-CHAIN-STATUS-DIGEST).

A pure async aggregation function that composes fetch_probes, the /health breaker
snapshot, cell-probe freshness, and OpenRouter credits into a stable stdout digest.
Designed for the Claude session-start hook — exit 0 always.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import httpx
import typer

from ferova.health.credits import fetch_openrouter_credits
from ferova.health.model_health import (
    STATUS_EMPTY,
    STATUS_ERROR,
    STATUS_OK,
    STATUS_SKIPPED,
    STATUS_SLOW,
)
from ferova.health.store import fetch_probes
from ferova.llm_proxy.providers.cell_probe_store import fetch_cell_probes
from ferova.review.chain_health import chain_head

if TYPE_CHECKING:
    from ferova.llm_proxy.config.settings import Settings

_TIERS: tuple[str, ...] = ("opus", "sonnet", "haiku")


def _duration_human(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m = int(seconds // 60)
        s = int(seconds % 60)
        return f"{m}m{s}s" if s else f"{m}m"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    return f"{h}h{m}m" if m else f"{h}h"


def _age_human(dt: datetime, now: datetime) -> str:
    """Format the age of *dt* relative to *now* as a compact string."""
    delta = (now - dt).total_seconds()
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return f"{int(delta // 86400)}d ago"


async def build_chain_status(
    db_path: str,
    window_h: float,
    *,
    proxy_url: str,
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    """Build the chain-status digest string.

    Args:
        db_path: Path to the SQLite review DB.
        window_h: Look-back window in hours for probe aggregation.
        proxy_url: Base URL of the running llm_proxy
            (e.g. ``http://127.0.0.1:8082``).
        client: An ``httpx.AsyncClient`` injected for /health and credits
            requests.
        settings: The proxy Settings carrying tier chains, credits floor,
            and API key.

    Returns:
        The digest as a single string ready for stdout.
    """
    now = datetime.now(UTC)
    since = datetime.fromtimestamp(now.timestamp() - window_h * 3600, tz=UTC)
    db = Path(db_path)

    lines: list[str] = [f"chain-status ({window_h:.0f}h window)"]

    tier_chains: dict[str, str | None] = {
        "opus": settings.model_opus,
        "sonnet": settings.model_sonnet,
        "haiku": settings.model_haiku,
    }

    for tier in _TIERS:
        chain_value = tier_chains[tier]
        if not chain_value:
            lines.append(f"  {tier:<7} no chain configured")
            continue

        provider, head_model = chain_head(chain_value)
        head_ref = f"{provider}/{head_model}"

        if provider != "nvidia_nim":
            lines.append(f"  {tier:<7} head={head_ref}  UNMONITORED (probe skips non-NIM heads)")
            continue

        rows = fetch_probes(db, since=since, tier=tier)

        active = [r for r in rows if r.status != STATUS_SKIPPED]
        if not active:
            lines.append(f"  {tier:<7} head={head_ref}  no probes in window")
            continue

        n = len(active)
        ok_n = sum(1 for r in active if r.status == STATUS_OK)
        slow_n = sum(1 for r in active if r.status == STATUS_SLOW)
        err_n = sum(1 for r in active if r.status in (STATUS_ERROR, STATUS_EMPTY))

        ok_pct = round(ok_n / n * 100)
        slow_pct = round(slow_n / n * 100)
        err_pct = round(err_n / n * 100)
        total = ok_pct + slow_pct + err_pct
        if total != 100:
            err_pct += 100 - total

        parts = [f"{ok_pct}% ok · {slow_pct}% slow · {err_pct}% err  (n={n}"]
        if slow_n > 0:
            slow_latencies = [
                r.latency_s for r in active if r.status == STATUS_SLOW and r.latency_s is not None
            ]
            if slow_latencies:
                avg_slow = sum(slow_latencies) / len(slow_latencies)
                parts.append(f", avg slow {avg_slow:.1f}s")
        parts.append(")")
        mix = "".join(parts)
        lines.append(f"  {tier:<7} head={head_ref}  {mix}")

    breaker_lines = await _build_breaker_lines(proxy_url, client, tier_chains)
    lines.extend(breaker_lines)

    cell_line = await _build_cell_line(db)
    lines.append(cell_line)

    credits_line = await _build_credits_line(client, settings)
    lines.append(credits_line)

    return "\n".join(lines) + "\n"


async def _build_breaker_lines(
    proxy_url: str,
    client: httpx.AsyncClient,
    tier_chains: dict[str, str | None],
) -> list[str]:
    """Fetch /health and render breaker lines, one per tripped ref."""
    try:
        resp = await client.get(f"{proxy_url.rstrip('/')}/health", timeout=5.0)
    except Exception:
        return ["  proxy: unreachable (breaker state unknown)"]

    if resp.status_code < 200 or resp.status_code >= 300:
        return [f"  proxy: unreachable (breaker state unknown, status {resp.status_code})"]

    try:
        body = resp.json()
    except ValueError:
        return ["  proxy: unreachable (breaker state unknown)"]

    if not isinstance(body, dict):
        return ["  proxy: unreachable (breaker state unknown)"]

    breaker_entries = body.get("breaker")
    if not isinstance(breaker_entries, list):
        return ["  proxy: reachable"]

    lines: list[str] = ["  proxy: reachable"]

    tier_ref_map: dict[str, set[str]] = {}
    for tier, chain_value in tier_chains.items():
        if not chain_value:
            continue
        refs = {entry.strip() for entry in chain_value.split(",")}
        tier_ref_map[tier] = refs

    all_known_refs: set[str] = set()
    for refs in tier_ref_map.values():
        all_known_refs.update(refs)

    for entry in breaker_entries:
        ref = entry.get("ref", "")
        reason = entry.get("reason", "")
        ttl = entry.get("ttl_remaining_s", 0)
        failures = entry.get("consecutive_failures", 0)
        duration = _duration_human(ttl)

        matched_tier: str | None = None
        for tier, refs in tier_ref_map.items():
            if ref in refs:
                matched_tier = tier
                break

        if matched_tier is not None:
            lines.append(
                f"  breaker: {matched_tier} {ref} quarantined {duration} ({reason} x{failures})"
            )
        else:
            lines.append(
                f"  breaker (unchained): {ref} quarantined {duration} ({reason} x{failures})"
            )

    return lines


async def _build_cell_line(db: Path) -> str:
    """Fetch the newest cell probe and render its age."""
    rows = fetch_cell_probes(db, limit=1)
    if not rows:
        return "  cells:   no probes recorded"
    newest = rows[0].recorded_at
    now = datetime.now(UTC)
    age = _age_human(newest, now)
    return f"  cells:   newest {age}"


async def _build_credits_line(
    client: httpx.AsyncClient,
    settings: Settings,
) -> str:
    """Fetch OpenRouter credits and render the line."""
    if not settings.open_router_api_key:
        return "  credits: skipped (no key)"

    snapshot = await fetch_openrouter_credits(settings.open_router_api_key, client=client)
    if snapshot is None:
        return "  credits: unavailable"

    remaining = snapshot.remaining
    floor = settings.credits_floor_usd
    flag = " LOW" if remaining < floor else ""
    return f"  credits: open_router remaining={remaining} floor={floor}{flag}"


def chain_status(
    window_hours: float | None = typer.Option(
        None,
        "--window-hours",
        help="Look-back window in hours (default: settings.chain_status_window_h).",
    ),
    db_path: str | None = typer.Option(
        None,
        "--db-path",
        help="Override the SQLite path (default: the configured review DB).",
    ),
    proxy_url: str = typer.Option(
        "http://127.0.0.1:8082",
        "--proxy-url",
        help="Base URL of the running llm_proxy.",
    ),
) -> None:
    """Print the chain-status digest and always exit 0.

    Every data source degrades to an explicit ``unavailable`` line —
    this command is a surface, not a gate, so a broken venv can never
    block a Claude session.
    """
    import asyncio

    from ..core.config import get_settings
    from ..llm_proxy.config.settings import Settings as LSettings

    settings = LSettings()
    window = window_hours if window_hours is not None else settings.chain_status_window_h
    target_db = db_path if db_path else get_settings().db_path

    async def _run() -> str:
        async with httpx.AsyncClient() as client:
            return await build_chain_status(
                target_db,
                window,
                proxy_url=proxy_url,
                client=client,
                settings=settings,
            )

    try:
        digest = asyncio.run(_run())
        typer.echo(digest, nl=False)
    except Exception:
        typer.echo("chain-status: unavailable")
