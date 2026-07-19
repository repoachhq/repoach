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
from ferova.health.model_health import STATUS_OK, STATUS_SKIPPED, STATUS_SLOW
from ferova.health.store import fetch_probes
from ferova.llm_proxy.providers.cell_probe_store import fetch_cell_probes
from ferova.review.chain_health import chain_head

if TYPE_CHECKING:
    from ferova.llm_proxy.config.settings import Settings


def _duration_human(seconds: float) -> str:
    """Format a duration in seconds as a compact human-readable string."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        m, s = int(seconds // 60), int(seconds % 60)
        return f"{m}m{s}s" if s else f"{m}m"
    h, m = int(seconds // 3600), int((seconds % 3600) // 60)
    return f"{h}h{m}m" if m else f"{h}h"


def _age_human(dt: datetime, now: datetime) -> str:
    """Format the age of *dt* relative to *now* as a compact string."""
    delta = (now - dt).total_seconds()
    for divisor, cap, unit in ((1, 60, "s"), (60, 3600, "m"), (3600, 86400, "h")):
        if delta < cap:
            return f"{int(delta // divisor)}{unit} ago"
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
        proxy_url: Base URL of the running llm_proxy.
        client: Injected ``httpx.AsyncClient`` for /health and credits.
        settings: Proxy Settings carrying tier chains, floor, and key.

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

    for tier, chain_value in tier_chains.items():
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
        ok_pct = round(sum(1 for r in active if r.status == STATUS_OK) / n * 100)
        slow_pct = round(sum(1 for r in active if r.status == STATUS_SLOW) / n * 100)
        err_pct = 100 - ok_pct - slow_pct

        mix = f"{ok_pct}% ok · {slow_pct}% slow · {err_pct}% err  (n={n}"
        slow_lat = [
            r.latency_s for r in active if r.status == STATUS_SLOW and r.latency_s is not None
        ]
        if slow_lat:
            mix += f", avg slow {sum(slow_lat) / len(slow_lat):.1f}s"
        lines.append(f"  {tier:<7} head={head_ref}  {mix})")

    lines.extend(await _build_breaker_lines(proxy_url, client, tier_chains))

    cells = fetch_cell_probes(db, limit=1)
    if cells:
        lines.append(f"  cells:   newest {_age_human(cells[0].recorded_at, now)}")
    else:
        lines.append("  cells:   no probes recorded")

    if not settings.open_router_api_key:
        lines.append("  credits: skipped (no key)")
    else:
        snapshot = await fetch_openrouter_credits(settings.open_router_api_key, client=client)
        if snapshot is None:
            lines.append("  credits: unavailable")
        else:
            floor = settings.credits_floor_usd
            flag = " LOW" if snapshot.remaining < floor else ""
            lines.append(
                f"  credits: open_router remaining={snapshot.remaining} floor={floor}{flag}"
            )

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
    lines: list[str] = ["  proxy: reachable"]
    if not isinstance(breaker_entries, list):
        return lines

    tier_ref_map = {
        tier: {ref.strip() for ref in chain_value.split(",")}
        for tier, chain_value in tier_chains.items()
        if chain_value
    }

    for entry in breaker_entries:
        ref = entry.get("ref", "")
        reason = entry.get("reason", "")
        duration = _duration_human(entry.get("ttl_remaining_s", 0))
        failures = entry.get("consecutive_failures", 0)
        matched = next((tier for tier, refs in tier_ref_map.items() if ref in refs), None)
        label = f"breaker: {matched}" if matched else "breaker (unchained):"
        lines.append(f"  {label} {ref} quarantined {duration} ({reason} x{failures})")

    return lines


def chain_status(
    window_hours: float | None = typer.Option(
        None, "--window-hours", help="Look-back window in hours (default: settings)."
    ),
    db_path: str | None = typer.Option(
        None, "--db-path", help="Override the SQLite path (default: the review DB)."
    ),
    proxy_url: str = typer.Option(
        "http://127.0.0.1:8082", "--proxy-url", help="Base URL of the running llm_proxy."
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
                target_db, window, proxy_url=proxy_url, client=client, settings=settings
            )

    try:
        digest = asyncio.run(_run())
        typer.echo(digest, nl=False)
    except Exception:
        typer.echo("chain-status: unavailable")
