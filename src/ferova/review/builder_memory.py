"""Builder-scoped agentmemory orchestration (SP-BUILDER-MEMORY).

The BUILD agents (Planner + Developer) recall relevant lessons before
planning and record what they learned after building, all scoped to
``project=builder``. Keeping that loop here makes the wiring in
``planner.py`` / ``dev_runner.py`` one line each and the rendering /
composition logic unit-testable without the network.
"""

from __future__ import annotations

from ..core.config import get_settings
from ..memory import agentmemory_client

BUILDER_PROJECT = "builder"

SEED_LESSONS: tuple[str, ...] = (
    "Integration-test SQLite DBs must live OUTSIDE the repo tree — `git clean -fd` "
    "between steps eats anything under it.",
    "Every test a plan step promises must be CREATED by that same step; the gate "
    "reverts a step whose promised test file is absent. No forward references.",
    "Launch `ferova develop` FROM the checkout that carries the spec — load_spec "
    "reads cwd before the branch is created.",
    "The proxy is an editable install: merge develop into the impl branch (or restart) "
    "so it imports the current client code, not a stale checkout.",
    "Specs touching prompts/review/* or .github/workflows/* are force-majeure — the "
    "path whitelist forbids the bots; hand-ship them.",
    "ruff auto-fixes first in the gate, so a test fixture needs a non-autofixable "
    "failure (e.g. F821) to exercise a red step.",
    "Keep each plan step self-consistent: emit the code AND its test in the same step "
    "so the promised-tests gate passes.",
)


def recall_builder_lessons(query: str) -> list[str]:
    """Recall builder-scoped lessons relevant to *query*.

    Args:
        query: Free-text query (typically the spec id + its opening lines).

    Returns:
        Recalled lesson strings, or ``[]`` when ``builder_memory_enabled``
        is false or the agentmemory service is unreachable.
    """
    settings = get_settings()
    if not settings.builder_memory_enabled:
        return []
    return agentmemory_client.recall(
        query, project=BUILDER_PROJECT, base_url=settings.agentmemory_url
    )


def lessons_section(lessons: list[str]) -> str:
    """Render recalled lessons as a markdown block to append to a spec.

    Returns ``""`` for an empty list so the caller can append
    unconditionally without a guard.
    """
    if not lessons:
        return ""
    bullets = "\n".join(f"- {lesson}" for lesson in lessons)
    return (
        "\n\n## Lessons from past builds (agentmemory)\n"
        "Recalled from prior builds (not part of this spec) — apply where relevant.\n"
        f"{bullets}\n"
    )


def remember_build_outcome(
    spec_id: str,
    *,
    pushed: bool,
    no_op_reason: str | None,
    n_steps: int,
) -> bool:
    """Record one build's outcome as a builder-scoped memory.

    Args:
        spec_id: The spec the build implemented.
        pushed: Whether the build pushed a branch (i.e. succeeded).
        no_op_reason: The stall reason when the build did not push.
        n_steps: Plan steps completed.

    Returns:
        ``True`` when the memory was written, ``False`` when
        ``builder_memory_enabled`` is false or the write failed.
    """
    settings = get_settings()
    if not settings.builder_memory_enabled:
        return False
    if pushed:
        content = f"Built {spec_id}: pushed, {n_steps} step(s) green."
    elif no_op_reason:
        content = f"{spec_id} stalled: {no_op_reason[:200]}"
    else:
        content = f"{spec_id} produced no push ({n_steps} step(s))."
    return agentmemory_client.remember(
        content, project=BUILDER_PROJECT, base_url=settings.agentmemory_url
    )


def seed_builder_memory() -> int:
    """Write the curated :data:`SEED_LESSONS` into the builder scope.

    Returns the number of lessons the service accepted.
    """
    settings = get_settings()
    written = 0
    for lesson in SEED_LESSONS:
        if agentmemory_client.remember(
            lesson, project=BUILDER_PROJECT, base_url=settings.agentmemory_url
        ):
            written += 1
    return written
