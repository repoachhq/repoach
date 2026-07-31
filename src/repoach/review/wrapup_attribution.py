"""Attribute a wrap-up test failure to the plan step that introduced it.

SP-DEV-WRAPUP-ATTRIBUTION: when the wrap-up full unit suite goes red on a
selector no plan step promised, the session used to shrug (``fixes_applied:
0``, refuse push) with no clue which step broke it. This module provides the
pure orchestration core that names the introducing step: given a selector and
the plan's ordered step commits (base first), it walks the commits asking an
injectable ``run_selector`` callable whether the selector is green at each
one, and reports the first commit where it turns red.

This module performs no I/O itself and knows nothing about git, pytest, or
worktrees — :func:`attribute_failure_to_step` only calls the ``run_selector``
callable it is given. ``dev_runner.py`` wires a real git-worktree-backed
runner into this helper in a later step of this action plan.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class StepCommit:
    """One commit on the session's implementation branch.

    Attributes:
        index: The plan step index this commit corresponds to. ``0`` is a
            reserved convention for the plan base (the branch point before
            any step ran) — it does not name a real step.
        title: Human-readable title (the step's title, or ``"plan base"``
            for index ``0``).
        sha: The commit's git SHA (or any ref the injected ``run_selector``
            understands).
    """

    index: int
    title: str
    sha: str


@dataclass(frozen=True)
class AttributionOutcome:
    """Result of :func:`attribute_failure_to_step` for one selector.

    Attributes:
        selector: The pytest selector that was attributed.
        status: ``"introduced_by_step"`` when a step commit was named,
            ``"pre_existing"`` when the selector was already red at the
            plan base, or ``"error"`` when the runner raised or the walk
            was inconclusive.
        step: The blamed :class:`StepCommit` for ``"introduced_by_step"``
            (the introducing step) or ``"pre_existing"`` (the plan base).
            For ``"error"``, the :class:`StepCommit` being probed when
            ``run_selector`` raised (the base or the current step commit);
            ``None`` only for the no-exception "green at every recorded
            commit" inconclusive-exhaustion ``"error"``.
        error: The runner's error message (truncated) for ``"error"``;
            ``None`` otherwise.
    """

    selector: str
    status: Literal["introduced_by_step", "pre_existing", "error"]
    step: StepCommit | None = None
    error: str | None = None


def attribute_failure_to_step(
    selector: str,
    step_commits: list[StepCommit],
    *,
    run_selector: Callable[[str, str], bool],
) -> AttributionOutcome:
    """Name the step commit that first turned *selector* red.

    ``step_commits[0]`` is always the plan base (never a real step). The
    walk checks the base first — a selector already red at the base is
    ``pre_existing`` and no step is blamed for it (the whole point of
    checking base-first: a step must never be blamed for breakage it did
    not introduce). Only if the base is green does the walk proceed
    through ``step_commits[1:]`` in order, returning the first commit at
    which the selector goes red as the introducing step.

    Every call to *run_selector* is wrapped in a fail-closed try/except:
    any exception it raises (a crashed worktree, a pytest invocation
    error) immediately yields an ``"error"`` outcome carrying the
    exception message, rather than letting the exception propagate or
    guessing an attribution from partial data.

    Args:
        selector: The pytest selector (e.g. ``tests/unit/test_x.py::test_y``)
            that is red in the wrap-up full suite.
        step_commits: Ordered commits on the session's implementation
            branch, base first (index ``0``) followed by each step's
            commit in plan order.
        run_selector: A ``(sha, selector) -> bool`` callable returning
            ``True`` when *selector* is green at *sha*, ``False`` when
            red. Performs whatever I/O is needed (worktree checkout,
            pytest invocation); this function never calls it directly
            except through this parameter.

    Returns:
        An :class:`AttributionOutcome` naming the introducing step, the
        plan base (pre-existing failure), or an error.
    """
    base = step_commits[0]
    try:
        base_green = run_selector(base.sha, selector)
    except Exception as exc:
        return AttributionOutcome(selector, status="error", step=base, error=str(exc)[:300])

    if not base_green:
        return AttributionOutcome(selector, status="pre_existing", step=base)

    for commit in step_commits[1:]:
        try:
            commit_green = run_selector(commit.sha, selector)
        except Exception as exc:
            return AttributionOutcome(selector, status="error", step=commit, error=str(exc)[:300])
        if not commit_green:
            return AttributionOutcome(selector, status="introduced_by_step", step=commit)

    return AttributionOutcome(
        selector,
        status="error",
        error="selector green at every recorded commit; attribution inconclusive",
    )
