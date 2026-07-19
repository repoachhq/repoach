"""SP-REVIEWER-CHAIN-RENAME step 2 — Reviewer defaults use the new name.

Step 1 renamed the constant in ``agent_engine.agent_loop``; step 2
re-pointed its sole importer, the base ``Reviewer.model_chain``
default in ``review.reviewer``. This module pins that wiring: the
import resolves, the class default IS the renamed constant, and the
underlying value is byte-identical to the pre-rename behaviour
(``PROXY_SONNET_CHAIN``).
"""

from repoach.agent_engine.agent_loop import DEFAULT_REVIEWER_CHAIN, PROXY_SONNET_CHAIN
from repoach.review.reviewer import Reviewer


def test_reviewer_imports_resolve() -> None:
    """``review.reviewer`` imports cleanly against the renamed constant."""
    assert Reviewer is not None


def test_reviewer_model_chain_default_is_new_constant() -> None:
    """The base-class default resolves to ``DEFAULT_REVIEWER_CHAIN``."""
    assert Reviewer.model_chain is DEFAULT_REVIEWER_CHAIN


def test_reviewer_model_chain_default_value_unchanged() -> None:
    """The default's value is still ``PROXY_SONNET_CHAIN`` (no behaviour change)."""
    assert Reviewer.model_chain is PROXY_SONNET_CHAIN
