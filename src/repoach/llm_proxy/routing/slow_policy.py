"""Pure policy: is a completion slow enough to count as a breaker strike.

This module deliberately diverges from the offline-probe doctrine
("slowness is not a fault", ``src/repoach/llm_proxy/providers/
attribution.py``): the offline autopilot probes a model's capability in
isolation, where a slow-but-content-bearing response is still healthy
evidence. Live dispatch is different — it protects a caller waiting on
the wire. A completion that is both slow (past a latency gate) AND thin
(few tokens per second) is not capability evidence, it is a caller-facing
failure mode (the 2026-07-10 flapping-week signature: 12–15 s HTTP-200
completions that reset the breaker's containment on every success). This
module names that live-dispatch judgment as a small, total, pure
predicate so the services layer and the breaker's k-of-n history can
compose it without re-deriving the threshold logic.
"""

from __future__ import annotations


def is_slow_completion(
    latency_s: float,
    output_tokens: int | None,
    *,
    gate_s: float,
    tps_floor: float,
) -> bool:
    """Decide whether a completion counts as a slow-completion strike.

    A completion is slow iff it took longer than ``gate_s`` AND its
    tokens-per-second (``output_tokens / latency_s``) fell below
    ``tps_floor``. A completion with unknown output tokens is never
    flagged — the policy is conservative: it never strikes blind.

    Args:
        latency_s: The full-completion wall-clock latency in seconds.
        output_tokens: The final ``usage.output_tokens`` reported for the
            completion, or ``None`` when no usable delta reported it
            (e.g. a tool_use-only flow).
        gate_s: The latency threshold past which a completion is even
            considered for the tokens-per-second check.
        tps_floor: The tokens-per-second floor below which a completion
            past the gate counts as thin.

    Returns:
        ``True`` iff ``latency_s > gate_s`` and ``output_tokens`` is not
        ``None`` and ``output_tokens / latency_s < tps_floor``. Total and
        pure — never raises for any finite input, including
        ``latency_s <= 0``.
    """
    if latency_s <= gate_s:
        return False
    if output_tokens is None:
        return False
    if latency_s <= 0:
        return False
    return (output_tokens / latency_s) < tps_floor
