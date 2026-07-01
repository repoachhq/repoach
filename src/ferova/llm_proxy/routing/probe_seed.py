"""Seed the failover breaker from NIM probe history at startup.

`SP-PROXY-BREAKER-PROBE-SEED` (D2b). The 15-minute ``monitor-chains`` sweep
records each tier head's health to the ``nim_health_probe`` table. At proxy
startup this reads the latest probe per tier and, when it is degraded,
trips that tier's chain HEAD in the process breaker — so a model known dead
(an EOL ``410``) is skipped before the first live request pays its
round-trip. Reuses the reason-aware TTL (``ttl_for_reason``) so a probed
410 stays down for the terminal window while a stale transient error only
trips the short one.

The probe store lives in the neutral :mod:`ferova.health` leaf
(`SP-HEALTH-STORE-NEUTRALIZE`), so this import introduces no
``llm_proxy <-> review`` cycle.
"""

from __future__ import annotations

from pathlib import Path

from ferova.health.model_health import is_degraded
from ferova.health.store import fetch_probes
from ferova.llm_proxy.config.settings import Settings
from ferova.llm_proxy.routing import (
    RoutingTable,
    Tier,
    get_breaker,
    ttl_for_reason,
)

_TRANSIENT_REASON = "probe_degraded"
_TIER_VALUES: frozenset[str] = frozenset(tier.value for tier in Tier)


def parse_tier(value: str) -> Tier | None:
    """Map a probe ``tier`` string to a routing :class:`Tier`, or ``None``.

    Args:
        value: The probe row's tier (``opus`` / ``sonnet`` / ``haiku``).

    Returns:
        The matching :class:`Tier`, or ``None`` for an unrecognised value.
    """
    return Tier(value) if value in _TIER_VALUES else None


def reason_from_detail(detail: str) -> str:
    """Derive a failover reason from a probe's ``detail`` note.

    A ``410`` anywhere in the note marks an EOL model — the terminal
    ``provider_410`` reason (long TTL). Anything else degraded is treated
    as transient (short TTL), since a stale non-410 error self-clears.

    Args:
        detail: The probe row's short note (e.g. ``"http=410"``).

    Returns:
        ``provider_410`` for an EOL signal, else ``probe_degraded``.
    """
    return "provider_410" if "410" in detail else _TRANSIENT_REASON


def seed_breaker_from_probes(settings: Settings, *, now: float, db_path: Path) -> int:
    """Pre-trip the breaker for tiers whose latest probe is degraded.

    For each tier, the most recent probe (``fetch_probes`` returns rows
    newest-first) decides: a degraded one trips that tier's chain HEAD —
    the ref the router would try first — with a reason-aware TTL. A
    non-degraded latest probe leaves the tier untouched.

    Args:
        settings: The proxy settings (for the routing table + TTLs).
        now: A ``time.monotonic`` timestamp the trips are stamped with.
        db_path: The SQLite path holding the ``nim_health_probe`` table.

    Returns:
        The number of tier heads tripped.
    """
    table = RoutingTable.from_settings(settings)
    breaker = get_breaker()
    seen: set[Tier] = set()
    tripped = 0
    for row in fetch_probes(db_path):
        tier = parse_tier(row.tier)
        if tier is None or tier in seen:
            continue
        seen.add(tier)
        if not is_degraded(row.status):
            continue
        chain = table.chains.get(tier)
        if chain is None:
            continue
        head = chain.refs[0]
        reason = reason_from_detail(row.detail)
        ttl_s = ttl_for_reason(
            reason,
            default_ttl_s=settings.breaker_ttl_s,
            terminal_ttl_s=settings.breaker_ttl_terminal_s,
        )
        breaker.trip(head, now=now, ttl_s=ttl_s)
        tripped += 1
    return tripped
