"""The NIM probe health record and its status vocabulary (neutral leaf).

`SP-HEALTH-STORE-NEUTRALIZE`. :class:`ModelHealth` and the status
constants live here — a pure module with no ``llm_proxy`` or ``review``
import — so both the review prober (`review.chain_health`) and the
llm_proxy breaker-seed can share them without an import cycle.
"""

from __future__ import annotations

from dataclasses import dataclass

STATUS_OK = "ok"
STATUS_SLOW = "slow"
STATUS_EMPTY = "empty"
STATUS_ERROR = "error"
STATUS_SKIPPED = "skipped"

_DEGRADED_STATUSES: frozenset[str] = frozenset({STATUS_EMPTY, STATUS_ERROR})


@dataclass(frozen=True, slots=True)
class ModelHealth:
    """Health of one chain head model after a single probe.

    Attributes:
        tier: Capability tier (``opus`` / ``sonnet`` / ``haiku`` /
            ``coder``).
        model: The probed model id, or ``provider/model`` for a skipped
            non-NIM head, or ``""`` when no chain is configured.
        status: One of ``ok`` / ``slow`` / ``empty`` / ``error`` /
            ``skipped``.
        latency_s: Wall-clock seconds for the probe, or ``None`` when no
            request completed (skipped or transport error).
        content_chars: Length of the returned assistant text.
        detail: A short human note — the content preview on success, the
            error class on failure, or the skip reason.
    """

    tier: str
    model: str
    status: str
    latency_s: float | None
    content_chars: int
    detail: str


def is_degraded(status: str) -> bool:
    """Return whether a status means the head needs operator attention."""
    return status in _DEGRADED_STATUSES
