"""In-process health breaker for the failover chain.

Closes the open health loop: a provider/model that fails at dispatch
(transport error, 410, timeout, empty completion) is tripped here, and
the router filters it out of subsequent chains until a short TTL lapses.
A dead or cold model therefore stops being re-tried at the chain head on
every request — the original diagnosis of the routing redesign — while a
recovered one re-enters automatically once its cool-down expires.

The breaker is a process-level singleton (:func:`get_breaker`) so its
state survives across requests; ``time.monotonic`` timestamps are passed
in by the caller, keeping the state itself clock-free and testable.
"""

from __future__ import annotations

from dataclasses import dataclass

from ferova.llm_proxy.routing.refs import ModelRef

TERMINAL_REASONS: frozenset[str] = frozenset({"provider_410"})
"""Failover reasons that signal a permanent upstream death, not a flap.

Kept deliberately tight: only HTTP ``410 Gone`` — the NIM end-of-life
signal (a retired model id, as ``meta/llama-3.1-405b`` was) — earns the
long cool-down. A timeout, a 5xx, a rate-limit or an empty completion are
all recoverable and must keep the short transient TTL so a momentarily
cold model is re-probed soon.
"""

QUARANTINE_REASONS: frozenset[str] = frozenset(
    {
        "auth_failed",
        "provider_401",
        "provider_402",
        "provider_403",
        "provider_404",
    }
)
"""Failover reasons that signal a permanent config/account fault.

An auth failure, a 401 (bad key), a 402 (no credits), a 403 (forbidden),
or a 404 (bad model id) will not self-heal — the operator must fund the
account, rotate the key, or fix the model id.  These earn the quarantine
TTL on the first occurrence so dispatch stops paying the round-trip every
transient window.
"""


@dataclass(frozen=True, slots=True)
class BreakerEntry:
    """A read-only snapshot entry for a currently-down model ref."""

    ref: ModelRef
    reason: str
    ttl_remaining_s: float
    consecutive_failures: int


def ttl_for_reason(
    reason: str,
    *,
    default_ttl_s: float,
    terminal_ttl_s: float,
    quarantine_ttl_s: float | None = None,
) -> float:
    """Pick a breaker cool-down from the failover reason.

    Args:
        reason: The failover reason string emitted by
            ``_classify_failover_reason`` (e.g. ``provider_410``,
            ``timeout``, ``empty_completion``).
        default_ttl_s: Cool-down for a transient fault.
        terminal_ttl_s: Cool-down for a permanent (EOL) fault.
        quarantine_ttl_s: Cool-down for a permanent-config fault
            (auth, 401/402/403/404).  When ``None`` (backward-compatible
            default) quarantine-class reasons fall back to
            ``default_ttl_s`` so existing callers stay green.

    Returns:
        ``terminal_ttl_s`` when ``reason`` is a terminal death,
        ``quarantine_ttl_s`` when it is a quarantine-class reason
        (or ``default_ttl_s`` when ``quarantine_ttl_s`` is ``None``),
        otherwise ``default_ttl_s``. Pure and total — no clock, no I/O.
    """
    if reason in TERMINAL_REASONS:
        return terminal_ttl_s
    if reason in QUARANTINE_REASONS:
        return quarantine_ttl_s if quarantine_ttl_s is not None else default_ttl_s
    return default_ttl_s


def escalated_ttl(
    consecutive_failures: int,
    *,
    base_ttl_s: float,
    quarantine_ttl_s: float,
    threshold: int,
) -> float:
    """Escalate to the quarantine TTL when consecutive failures reach the threshold.

    Pure policy — no clock, no I/O.  Returns the larger of ``base_ttl_s``
    and ``quarantine_ttl_s`` when ``consecutive_failures >= threshold``,
    otherwise ``base_ttl_s`` unchanged.

    Args:
        consecutive_failures: The ref's current consecutive-failure count
            (as returned by :meth:`BreakerState.trip`).
        base_ttl_s: The TTL that would apply without escalation.
        quarantine_ttl_s: The long quarantine cool-down.
        threshold: How many consecutive failures trigger escalation.

    Returns:
        The effective TTL after applying escalation policy.
    """
    if consecutive_failures >= threshold:
        return max(base_ttl_s, quarantine_ttl_s)
    return base_ttl_s


class BreakerState:
    """Tracks which model references are currently tripped (down)."""

    def __init__(self) -> None:
        self._down_until: dict[ModelRef, float] = {}
        self._down_reason: dict[ModelRef, str] = {}
        self._consecutive_failures: dict[ModelRef, int] = {}

    def trip(self, ref: ModelRef, *, now: float, ttl_s: float, reason: str = "") -> int:
        """Mark ``ref`` down until ``now + ttl_s`` and record the reason.

        Extends an existing trip but never shortens it, so a repeated
        failure cannot accidentally bring a model back early.
        Increments and returns the ref's consecutive-failure count.

        Args:
            ref: The provider/model reference to trip.
            now: ``time.monotonic`` timestamp.
            ttl_s: Cool-down window in seconds.
            reason: The failover reason string (stored for snapshot).

        Returns:
            The ref's consecutive-failure count AFTER this trip.
        """
        until = now + ttl_s
        current = self._down_until.get(ref)
        if current is None or until > current:
            self._down_until[ref] = until
        self._down_reason[ref] = reason
        count = self._consecutive_failures.get(ref, 0) + 1
        self._consecutive_failures[ref] = count
        return count

    def recover(self, ref: ModelRef) -> None:
        """Clear ``ref`` immediately — it just served real content.

        Resets the trip, the stored reason, and the consecutive-failure
        counter so a subsequent failure starts a fresh count.
        """
        self._down_until.pop(ref, None)
        self._down_reason.pop(ref, None)
        self._consecutive_failures.pop(ref, None)

    def is_down(self, ref: ModelRef, now: float) -> bool:
        """Return whether ``ref`` is tripped at ``now``."""
        until = self._down_until.get(ref)
        return until is not None and until > now

    def down_refs(self, now: float) -> frozenset[ModelRef]:
        """Return the currently-down refs, pruning any whose TTL lapsed."""
        live = {ref: until for ref, until in self._down_until.items() if until > now}
        expired = self._down_until.keys() - live.keys()
        self._down_until = live
        for ref in expired:
            self._down_reason.pop(ref, None)
            self._consecutive_failures.pop(ref, None)
        return frozenset(self._down_until)

    def snapshot(self, now: float) -> list[BreakerEntry]:
        """Return a read-only list of entries for each currently-down ref.

        Args:
            now: ``time.monotonic`` timestamp for computing remaining TTL.

        Returns:
            A list of :class:`BreakerEntry` — one per ref whose TTL has
            not yet lapsed at ``now``.
        """
        entries: list[BreakerEntry] = []
        for ref, until in self._down_until.items():
            if until > now:
                entries.append(
                    BreakerEntry(
                        ref=ref,
                        reason=self._down_reason.get(ref, ""),
                        ttl_remaining_s=until - now,
                        consecutive_failures=self._consecutive_failures.get(ref, 0),
                    )
                )
        return entries

    def clear(self) -> None:
        """Drop all trips (used for test hermeticity)."""
        self._down_until.clear()
        self._down_reason.clear()
        self._consecutive_failures.clear()


_BREAKER = BreakerState()


def get_breaker() -> BreakerState:
    """Return the process-level breaker singleton."""
    return _BREAKER


def reset_breaker() -> None:
    """Clear the singleton's state — the unit suite resets per test."""
    _BREAKER.clear()
