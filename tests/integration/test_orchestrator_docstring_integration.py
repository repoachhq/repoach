"""Pin SP-ORCH-DOCSTRING acceptance via the import path.

This integration test imports ``repoach.review.orchestrator`` and asserts
on the live module ``__doc__``, complementing the unit-level source-file
pins in ``tests/unit/test_orchestrator_docstring.py``.  Together they
guard against the retired verdict-first narrative silently returning.
"""

from __future__ import annotations

import repoach.review.orchestrator as orchestrator

_RETIRED_SUBSTRINGS = (
    "Aggregates their verdicts",
    "does **not** auto-merge",
    "run_coder_response",
)

_REQUIRED_TOKENS = ("findings", "derived", "report-only")


def test_orchestrator_docstring_truthful_via_imported_module() -> None:
    """The live module __doc__ drops the retired narrative and describes the ledger pipeline."""
    doc = orchestrator.__doc__
    assert isinstance(doc, str), "orchestrator module must expose a __doc__ string"

    for needle in _RETIRED_SUBSTRINGS:
        assert needle not in doc, (
            f"orchestrator.__doc__ still contains the retired phrase {needle!r}; "
            "the evidence-first docstring has regressed."
        )

    for needle in _REQUIRED_TOKENS:
        assert needle in doc, (
            f"orchestrator.__doc__ is missing the required token {needle!r}; "
            "the evidence-first pipeline description is incomplete."
        )
