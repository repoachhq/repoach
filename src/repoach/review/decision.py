"""Decision engine — planned chain mutations from faults + scoreboard (SP-CHAINPILOT-DECISION).

Phase 3b of the Chain Autopilot arc — the policy layer 2d and 3a declined to
carry. It reads the 3a fault verdicts and the 2d scoreboard and emits a list of
planned mutations to the chains (evict / drop-provider / demote / promote /
advise). Pure: it plans, it does not write — 3d applies.

Two signals drive a mutation, kept strictly separate. **Fault** (3a) maps 1:1,
deterministically, to a structural action — a model failing everywhere is
evicted, a model failing on one provider has that provider dropped, a starved
model is *advised* (our config to fix, never a punishment). **Quality** (2d) is
ordinal and statistically guarded: within a rate metric's candidate set we
demote the worst and promote the best, but only when they are separated beyond
sampling noise (a binomial standard-error test), so the bar emerges from the
data rather than an invented threshold. Cold-start insertion of a never-seen
model is a structural operation (it needs chain + matrix context) and lives in
the apply layer (3d), not here.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from math import sqrt

from repoach.llm_proxy.providers.attribution import CellFault, FaultClass
from repoach.review.perf_aggregate import ModelPerformance

DEFAULT_SIGMA: float = 1.0
"""Standard-error multiplier a quality gap must clear to move a model.

A statistical confidence factor, not a calibration threshold: the bar it sets
scales with the sample sizes, so no absolute success-rate cutoff is invented.
"""

_HIGHER_IS_BETTER: dict[str, bool] = {
    "coder_ci_green": True,
    "reviewer_precision": True,
    "coder_stuck": False,
}
"""The rate metrics eligible for gated demote/promote, and their good direction.

``coder_rounds_to_green`` is intentionally absent — it is a mean of counts with
no exposed variance, so its noise cannot be gauged for the separation test.
"""

__all__ = [
    "DEFAULT_SIGMA",
    "MutationKind",
    "PlannedMutation",
    "plan_mutations",
]


class MutationKind(StrEnum):
    """The kind of chain change a :class:`PlannedMutation` proposes.

    The decision engine (3b) emits only the first five; ``COLD_START`` is emitted
    by the apply layer (3d) for a never-seen model trialled into a chain — kept a
    distinct kind so the audit log never conflates a cold-start insertion with a
    quality-driven ``promote``.
    """

    EVICT_MODEL = "evict_model"
    DROP_PROVIDER = "drop_provider"
    DEMOTE = "demote"
    PROMOTE = "promote"
    ADVISE = "advise"
    COLD_START = "cold_start"


@dataclass(frozen=True)
class PlannedMutation:
    """One planned, not-yet-applied change to the chains.

    Attributes:
        kind: The :class:`MutationKind`.
        model: The model the mutation targets.
        provider: The provider — the one to drop (``drop_provider``) or the one a
            ``cold_start`` is trialled on; ``None`` otherwise.
        metric: The rate metric that drove a ``demote`` / ``promote``, else
            ``None``.
        reason: A short human-readable justification.
    """

    kind: MutationKind
    model: str
    provider: str | None
    metric: str | None
    reason: str


def _separated(p_a: float, n_a: int, p_b: float, n_b: int, *, sigma: float) -> bool:
    """Whether two rates differ beyond ``sigma`` combined binomial standard errors."""
    if n_a <= 0 or n_b <= 0:
        return False
    se = sqrt(p_a * (1.0 - p_a) / n_a + p_b * (1.0 - p_b) / n_b)
    return abs(p_a - p_b) > sigma * se


def _fault_mutations(faults: Sequence[CellFault]) -> list[PlannedMutation]:
    """Map fault verdicts to deterministic structural mutations (deduped)."""
    evicted: dict[str, PlannedMutation] = {}
    dropped: dict[tuple[str, str], PlannedMutation] = {}
    advised: dict[str, PlannedMutation] = {}
    for fault in faults:
        if fault.fault is FaultClass.MODEL_FAULT:
            evicted.setdefault(
                fault.model_id,
                PlannedMutation(MutationKind.EVICT_MODEL, fault.model_id, None, None, fault.reason),
            )
        elif fault.fault is FaultClass.PROVIDER_FAULT:
            dropped.setdefault(
                (fault.model_id, fault.provider_id),
                PlannedMutation(
                    MutationKind.DROP_PROVIDER,
                    fault.model_id,
                    fault.provider_id,
                    None,
                    fault.reason,
                ),
            )
        elif fault.fault is FaultClass.OUR_FAULT:
            advised.setdefault(
                fault.model_id,
                PlannedMutation(
                    MutationKind.ADVISE,
                    fault.model_id,
                    None,
                    None,
                    f"{fault.scope.value}: {fault.reason}",
                ),
            )
    return [*evicted.values(), *dropped.values(), *advised.values()]


def _quality_mutations(
    scoreboard: Sequence[ModelPerformance], *, sigma: float
) -> list[PlannedMutation]:
    """Ordinal, noise-guarded demote/promote per gated rate metric."""
    out: list[PlannedMutation] = []
    for metric, higher_is_better in _HIGHER_IS_BETTER.items():
        candidates: list[tuple[str, float, int]] = []
        for perf in scoreboard:
            for guarded in perf.metrics:
                if guarded.metric == metric and guarded.confident and guarded.value is not None:
                    candidates.append((perf.model, guarded.value, guarded.n))
        if len(candidates) < 2:
            continue

        sign = 1.0 if higher_is_better else -1.0
        best = max(candidates, key=lambda item, s=sign: s * item[1])
        worst = min(candidates, key=lambda item, s=sign: s * item[1])
        if best[0] == worst[0]:
            continue
        if not _separated(best[1], best[2], worst[1], worst[2], sigma=sigma):
            continue
        out.append(
            PlannedMutation(
                MutationKind.PROMOTE,
                best[0],
                None,
                metric,
                f"best confident {metric} ({best[1]:.3f}, n={best[2]}), separated beyond noise",
            )
        )
        out.append(
            PlannedMutation(
                MutationKind.DEMOTE,
                worst[0],
                None,
                metric,
                f"worst confident {metric} ({worst[1]:.3f}, n={worst[2]}), separated beyond noise",
            )
        )
    return out


def plan_mutations(
    faults: Sequence[CellFault],
    scoreboard: Sequence[ModelPerformance],
    *,
    sigma: float = DEFAULT_SIGMA,
) -> list[PlannedMutation]:
    """Plan the chain mutations implied by the faults and the scoreboard.

    Pure and I/O-free: the caller injects the 3a verdicts and the 2d scoreboard.
    Faults map deterministically to structural mutations; quality drives
    ordinal, noise-guarded demote/promote. Cold-start insertion of a new model
    is NOT decided here — it is a structural operation needing chain + matrix
    context and lives in the apply layer (3d).

    Args:
        faults: The per-cell fault verdicts from 3a.
        scoreboard: The per-model performance entries from 2d.
        sigma: The standard-error multiplier a quality gap must clear.

    Returns:
        The planned mutations, deterministically ordered by
        ``(kind, model, provider, metric)``.
    """
    mutations = [
        *_fault_mutations(faults),
        *_quality_mutations(scoreboard, sigma=sigma),
    ]
    return sorted(
        mutations,
        key=lambda m: (m.kind.value, m.model, m.provider or "", m.metric or ""),
    )
