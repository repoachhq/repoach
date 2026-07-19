"""SP-REVIEWER-CHAIN-RENAME integration — a concrete reviewer builds as before.

Constructs a concrete reviewer subclass exactly the way the
orchestrator does — no explicit ``model_chain=`` — and asserts the
resulting instance routes through ``PROXY_SONNET_CHAIN``, byte-for-byte
identical to the pre-rename behaviour. Construction is offline: the
``AgentLoop`` built in ``Reviewer.__init__`` performs no network call —
but it does require the proxy shared secret via the core ``Settings``
singleton, so the test provides one through the environment and resets
the singleton for the duration (env-hermetic: the first CI run at this
head failed for lack of a local ``.env``; ``monkeypatch`` restores the
original singleton afterwards).
"""

import pytest

import repoach.core.config as core_config
from repoach.agent_engine.agent_loop import PROXY_SONNET_CHAIN
from repoach.review.reviewer import Architect


def test_concrete_reviewer_default_chain_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An ``Architect()`` built with defaults routes via ``PROXY_SONNET_CHAIN``."""
    monkeypatch.setenv("FEROVA_ANTHROPIC_AUTH_TOKEN", "test-shared-secret")
    monkeypatch.setattr(core_config, "_settings", None)
    architect = Architect()
    assert architect.model_chain is PROXY_SONNET_CHAIN
