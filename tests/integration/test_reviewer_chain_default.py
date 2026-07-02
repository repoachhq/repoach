"""SP-REVIEWER-CHAIN-RENAME integration — a concrete reviewer builds as before.

Constructs a concrete reviewer subclass exactly the way the
orchestrator does — no explicit ``model_chain=`` — and asserts the
resulting instance routes through ``PROXY_SONNET_CHAIN``, byte-for-byte
identical to the pre-rename behaviour. Construction is offline: the
``AgentLoop`` built in ``Reviewer.__init__`` is pure configuration and
performs no network call.
"""

from ferova.agent_engine.agent_loop import PROXY_SONNET_CHAIN
from ferova.review.reviewer import Architect


def test_concrete_reviewer_default_chain_unchanged() -> None:
    """An ``Architect()`` built with defaults routes via ``PROXY_SONNET_CHAIN``."""
    architect = Architect()
    assert architect.model_chain is PROXY_SONNET_CHAIN
