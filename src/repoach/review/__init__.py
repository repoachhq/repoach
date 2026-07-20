"""Review-bot team for Repoach pull requests.

Five specialised bots, all running on NVIDIA NIM via :mod:`agent_engine.agent_loop`
(zero Anthropic / Claude Code Pro quota consumption):

* :class:`Architect` — coupling, naming, separation of concerns.
* :class:`Sentinel` — security, gates, secrets, prompt injection.
* :class:`Tester` — test coverage, edge cases, fixtures.
* :class:`Scribe` — docstrings, commit messages, doc consistency.
* :class:`Coder` — owner of the branch; resolves the reviewers' findings.

The :class:`ReviewTeamOrchestrator` runs the four reviewers in parallel
on a given PR diff, aggregates their verdicts, and (optionally) hands
the findings to the Coder for resolution.
"""

from .reviewer import (
    Architect,
    BotRole,
    Coder,
    Reviewer,
    ReviewerOutcome,
    ReviewVerdict,
    Scribe,
    Sentinel,
    Tester,
)

__all__ = [
    "Architect",
    "BotRole",
    "Coder",
    "ReviewVerdict",
    "Reviewer",
    "ReviewerOutcome",
    "Scribe",
    "Sentinel",
    "Tester",
]
