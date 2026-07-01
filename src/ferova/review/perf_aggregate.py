"""Per-model performance scoreboard (SP-CHAINPILOT-PERF-AGGREGATE).

Phase 2d of the Chain Autopilot arc — the convergence point of brick 4. It
assembles, per model, the live posteriors from 2b (coder) and 2c (reviewer),
each minimum-sample guarded, alongside the benchmark prior rank as a separate
cold-start signal. The observatory's scoreboard — the input the decision engine
(Phase 3b) reads.

By design (agreed 2026-06-25) it **exposes, it does not fuse**. The benchmark
prior is a generic quality (intelligence index, SWE-V, …); the posteriors are
task success rates on our own work (CI-green, reviewer precision). They are not
the same quantity, so blending them numerically would have to assume and
calibrate a quality->rate correspondence — the synthetic mapping the project
avoids. Per Principle 3, the prior is a cold-start rank for unseen models, the
posterior is the truth once samples exist, and the actual policy (blend /
threshold / evict) lives in the decision engine. Each dimension keeps its own
metric — there is no single score.

This leaf lives in ``review/`` because it imports both the ``review/``
posteriors and the ``llm_proxy/`` prior, and only the ``review -> llm_proxy``
direction is acyclic. Its four governed imports are declared in the spec's
``depends_on``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ferova.llm_proxy.providers.benchmark_equivalences import EquivalenceTable
from ferova.llm_proxy.providers.benchmark_prior import BenchmarkRanking

from .coder_outcomes import harvest_coder_outcomes
from .reviewer_outcomes import harvest_reviewer_outcomes

DEFAULT_MIN_SAMPLE: int = 5
"""Default sample size below which a posterior dimension is low-confidence.

A statistical confidence guard, not a calibration threshold; ``n`` is exposed
raw on every :class:`GuardedMetric` so the decision engine may re-guard.
"""

__all__ = [
    "DEFAULT_MIN_SAMPLE",
    "GuardedMetric",
    "ModelPerformance",
    "aggregate_model_performance",
]


@dataclass(frozen=True)
class GuardedMetric:
    """One posterior dimension for a model, with its sample-size guard.

    Attributes:
        metric: The dimension key (``coder_ci_green`` / ``coder_stuck`` /
            ``coder_rounds_to_green`` / ``reviewer_precision``).
        value: The live posterior value, or ``None`` when the harvest had no
            sample to compute it.
        n: The number of observations backing the value.
        confident: Whether ``n`` clears the minimum-sample bar.
    """

    metric: str
    value: float | None
    n: int
    confident: bool


@dataclass(frozen=True)
class ModelPerformance:
    """One model's guarded posteriors plus its cold-start benchmark prior.

    Attributes:
        model: The model identifier (as the harvests record ``model_used``).
        prior_rank: The model's best benchmark rank on the requested prior
            metric, or ``None`` when no prior is resolvable (no inputs given,
            or the model is unseen in the benchmark).
        prior_score: The score paired with ``prior_rank``, or ``None``.
        metrics: The model's guarded posterior dimensions; only the dimensions
            it has data for are present.
    """

    model: str
    prior_rank: int | None
    prior_score: float | None
    metrics: tuple[GuardedMetric, ...]


def _guard(metric: str, value: float | None, n: int, min_sample: int) -> GuardedMetric:
    """Wrap a harvested ``value``/``n`` into a sample-guarded dimension."""
    return GuardedMetric(metric=metric, value=value, n=n, confident=n >= min_sample)


def _resolve_prior(
    model: str,
    ranking: BenchmarkRanking | None,
    equivalences: EquivalenceTable | None,
    prior_metric: str | None,
) -> tuple[int | None, int | float | None]:
    """Resolve a model's best cold-start ``(rank, score)`` on ``prior_metric``.

    Returns ``(None, None)`` unless all three inputs are present and the model
    maps, via the equivalence table, to a benchmark alias ranked on
    ``prior_metric``; otherwise the lowest (best) rank across its aliases wins.
    Rank-less attribute entries (e.g. ``output_speed`` / ``price_blended``) are
    skipped, so a prior rank is only ever taken from a true ranking metric.
    """
    if ranking is None or equivalences is None or prior_metric is None:
        return None, None
    best: tuple[int, float] | None = None
    for alias in equivalences.aliases_for_model_id(model):
        for entry in ranking.entries_for_model(alias):
            if entry.metric != prior_metric or entry.rank is None:
                continue
            if best is None or entry.rank < best[0]:
                best = (entry.rank, entry.score)
    if best is None:
        return None, None
    return best[0], best[1]


def aggregate_model_performance(
    db_path: Path,
    *,
    ranking: BenchmarkRanking | None = None,
    equivalences: EquivalenceTable | None = None,
    prior_metric: str | None = None,
    min_sample: int = DEFAULT_MIN_SAMPLE,
) -> list[ModelPerformance]:
    """Assemble the per-model scoreboard from the 2b/2c harvests and the prior.

    Pure and read-only. The coder and reviewer harvests own their own table
    initialisation (so a fresh DB yields ``[]``); this function composes their
    outputs verbatim, guards each dimension by ``min_sample``, and attaches the
    optional cold-start prior. It never fuses prior and posterior.

    Args:
        db_path: The review ledger SQLite database.
        ranking: An optional benchmark ranking for the cold-start prior.
        equivalences: An optional model-id-to-benchmark-name table.
        prior_metric: The benchmark metric to rank the prior on; required for
            any prior to be attached.
        min_sample: The confidence bar for ``GuardedMetric.confident``.

    Returns:
        One :class:`ModelPerformance` per model in the union of the two
        harvests, ordered by model id.
    """
    coder = {outcome.model: outcome for outcome in harvest_coder_outcomes(db_path)}
    reviewer = {outcome.model: outcome for outcome in harvest_reviewer_outcomes(db_path)}

    out: list[ModelPerformance] = []
    for model in sorted(set(coder) | set(reviewer)):
        metrics: list[GuardedMetric] = []
        co = coder.get(model)
        if co is not None:
            metrics.append(_guard("coder_ci_green", co.ci_green_rate, co.n_prs, min_sample))
            metrics.append(_guard("coder_stuck", co.stuck_rate, co.n_prs, min_sample))
            metrics.append(
                _guard("coder_rounds_to_green", co.avg_rounds_to_green, co.n_ci_green, min_sample)
            )
        ro = reviewer.get(model)
        if ro is not None:
            metrics.append(_guard("reviewer_precision", ro.precision, ro.n_settled, min_sample))
        prior_rank, prior_score = _resolve_prior(model, ranking, equivalences, prior_metric)
        out.append(
            ModelPerformance(
                model=model,
                prior_rank=prior_rank,
                prior_score=prior_score,
                metrics=tuple(metrics),
            )
        )
    return out
