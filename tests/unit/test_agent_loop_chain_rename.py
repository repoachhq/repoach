"""SP-REVIEWER-CHAIN-RENAME — pin the renamed reviewer-chain constant.

``agent_engine.agent_loop`` used to export a module-level constant
named ``DEFAULT_NIM_CHAIN`` even though its value was always
``PROXY_SONNET_CHAIN`` — the base default for the reviewers'
``model_chain``, not a NIM-only chain. This module pins the rename to
``DEFAULT_REVIEWER_CHAIN``: the new name is importable, its value is
unchanged, it is listed in ``__all__``, the old misleading name is gone
with no back-compat alias, and the base ``Reviewer.model_chain`` default
still resolves to ``PROXY_SONNET_CHAIN``.
"""

from repoach.agent_engine import agent_loop
from repoach.agent_engine.agent_loop import DEFAULT_REVIEWER_CHAIN, PROXY_SONNET_CHAIN
from repoach.review.reviewer import Reviewer


def test_default_reviewer_chain_importable() -> None:
    """The renamed constant imports cleanly from its module."""
    assert DEFAULT_REVIEWER_CHAIN is not None


def test_default_reviewer_chain_value_unchanged() -> None:
    """The rename is identifier-only — the value still is ``PROXY_SONNET_CHAIN``."""
    assert DEFAULT_REVIEWER_CHAIN is PROXY_SONNET_CHAIN


def test_default_reviewer_chain_in_all() -> None:
    """``__all__`` advertises the new name as public surface."""
    assert "DEFAULT_REVIEWER_CHAIN" in agent_loop.__all__


def test_old_default_nim_chain_removed() -> None:
    """The retired name is gone — no back-compat alias was kept."""
    assert not hasattr(agent_loop, "DEFAULT_NIM_CHAIN")


def test_reviewer_base_default_is_new_constant() -> None:
    """The base ``Reviewer.model_chain`` default uses the renamed constant."""
    assert Reviewer.model_chain is DEFAULT_REVIEWER_CHAIN


def test_reviewer_base_default_value_unchanged() -> None:
    """The reviewers' default chain is unchanged by the rename."""
    assert Reviewer.model_chain is PROXY_SONNET_CHAIN
