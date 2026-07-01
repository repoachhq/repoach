"""Review-scoped agentmemory recall (SP-REVIEW-MEMORY).

The review bench recalls its own hard-won trap lessons before
reviewing, scoped to ``project=review`` — the second scope beside
``builder`` on the same agentmemory service. Only the *recall* side
lives here: the automatic *remember* stays gated on the verified
findings ledger (review redesign slice 11), because learning from
unverified reviewer comments would teach the bench its own
hallucinations. Until then the scope holds the curated
:data:`SEED_REVIEW_LESSONS` plus whatever the operator adds by hand.
"""

from __future__ import annotations

from ..core.config import get_settings
from ..memory import agentmemory_client

REVIEW_PROJECT = "review"

SEED_REVIEW_LESSONS: tuple[str, ...] = (
    "Verify a flagged string is part of THIS diff before flagging it — pre-existing "
    "code (including French strings) is out of scope for the review.",
    "Never claim a docstring or docstring section is missing without reading the "
    "cited file — they are usually present.",
    "Never claim a test is missing without searching tests/ for the symbol — it usually exists.",
    "A COMMENT verdict must carry concrete, actionable asks; once an ask is "
    "addressed, do not repeat it next round.",
    "If the diff looks truncated, say so and review only what is visible — never "
    "extrapolate blockers from code you cannot see.",
    "Every comment must cite a file:line that exists in the diff under review.",
)


def recall_review_lessons(query: str) -> list[str]:
    """Recall review-scoped lessons relevant to *query*.

    Args:
        query: Free-text query (typically the PR title + changed paths).

    Returns:
        Recalled lesson strings, or ``[]`` when ``review_memory_enabled``
        is false or the agentmemory service is unreachable.
    """
    settings = get_settings()
    if not settings.review_memory_enabled:
        return []
    return agentmemory_client.recall(
        query, project=REVIEW_PROJECT, base_url=settings.agentmemory_url
    )


def review_lessons_section(lessons: list[str]) -> str:
    """Render recalled lessons as a markdown block appended to a prompt.

    Returns ``""`` for an empty list so the caller can append
    unconditionally without a guard.
    """
    if not lessons:
        return ""
    bullets = "\n".join(f"- {lesson}" for lesson in lessons)
    return (
        "\n\n## Review lessons (agentmemory)\n"
        "Hard-won traps from past reviews — verify before you flag.\n"
        f"{bullets}\n"
    )


def seed_review_memory() -> int:
    """Write the curated :data:`SEED_REVIEW_LESSONS` into the review scope.

    Returns the number of lessons the service accepted.
    """
    settings = get_settings()
    written = 0
    for lesson in SEED_REVIEW_LESSONS:
        if agentmemory_client.remember(
            lesson, project=REVIEW_PROJECT, base_url=settings.agentmemory_url
        ):
            written += 1
    return written
