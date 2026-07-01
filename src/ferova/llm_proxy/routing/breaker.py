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

from ferova.llm_proxy.routing.refs import ModelRef

TERMINAL_REASONS: frozenset[str] = frozenset({"provider_410"})
"""Failover reasons that signal a permanent upstream death, not a flap.

Kept deliberately tight: only HTTP ``410 Gone`` — the NIM end-of-life
signal (a retired model id, as ``meta/llama-3.1-405b`` was) — earns the
long cool-down. A timeout, a 5xx, a rate-limit or an empty completion are
all recoverable and must keep the short transient TTL so a momentarily
cold model is re-probed soon.
"""


def ttl_for_reason(reason: str, *, default_ttl_s: float, terminal_ttl_s: float) -> float:
    """Pick a breaker cool-down from the failover reason.

    Args:
        reason: The failover reason string emitted by
            ``_classify_failover_reason`` (e.g. ``provider_410``,
            ``timeout``, ``empty_completion``).
        default_ttl_s: Cool-down for a transient fault.
        terminal_ttl_s: Cool-down for a permanent (EOL) fault.

    Returns:
        ``terminal_ttl_s`` when ``reason`` is a terminal death,
        otherwise ``default_ttl_s``. Pure and total — no clock, no I/O.
    """
    if reason in TERMINAL_REASONS:
        return terminal_ttl_s
    return default_ttl_s


class BreakerState:
    """Tracks which model references are currently tripped (down)."""

    def __init__(self) -> None:
        self._down_until: dict[ModelRef, float] = {}

    def trip(self, ref: ModelRef, *, now: float, ttl_s: float) -> None:
        """Mark ``ref`` down until ``now + ttl_s``.

        Extends an existing trip but never shortens it, so a repeated
        failure cannot accidentally bring a model back early.
        """
        until = now + ttl_s
        current = self._down_until.get(ref)
        if current is None or until > current:
            self._down_until[ref] = until

    def recover(self, ref: ModelRef) -> None:
        """Clear ``ref`` immediately — it just served real content."""
        self._down_until.pop(ref, None)

    def is_down(self, ref: ModelRef, now: float) -> bool:
        """Return whether ``ref`` is tripped at ``now``."""
        until = self._down_until.get(ref)
        return until is not None and until > now

    def down_refs(self, now: float) -> frozenset[ModelRef]:
        """Return the currently-down refs, pruning any whose TTL lapsed."""
        self._down_until = {ref: until for ref, until in self._down_until.items() if until > now}
        return frozenset(self._down_until)

    def clear(self) -> None:
        """Drop all trips (used for test hermeticity)."""
        self._down_until.clear()


_BREAKER = BreakerState()


def get_breaker() -> BreakerState:
    """Return the process-level breaker singleton."""
    return _BREAKER


def reset_breaker() -> None:
    """Clear the singleton's state — the unit suite resets per test."""
    _BREAKER.clear()
