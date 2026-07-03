"""SP-REVIEWER-CHAIN-RENAME step 4 — no stale reference survives the rename.

End-to-end acceptance pins for the whole rename: the new constant is
importable and carries the unchanged ``PROXY_SONNET_CHAIN`` value, the
base ``Reviewer`` default agrees, and the old ``DEFAULT_NIM_CHAIN``
name is gone from the module surface with no back-compat alias.
"""

from ferova.agent_engine import agent_loop
from ferova.agent_engine.agent_loop import DEFAULT_REVIEWER_CHAIN, PROXY_SONNET_CHAIN
from ferova.review.reviewer import Reviewer


def test_default_reviewer_chain_resolves_to_proxy_sonnet() -> None:
    """The renamed constant is importable and equals ``PROXY_SONNET_CHAIN``."""
    assert DEFAULT_REVIEWER_CHAIN is PROXY_SONNET_CHAIN


def test_reviewer_base_default_is_proxy_sonnet() -> None:
    """The base ``Reviewer.model_chain`` default is ``PROXY_SONNET_CHAIN``."""
    assert Reviewer.model_chain is PROXY_SONNET_CHAIN


def test_no_default_nim_chain_attribute() -> None:
    """The misleading old name is fully retired — no back-compat alias."""
    assert not hasattr(agent_loop, "DEFAULT_NIM_CHAIN")
