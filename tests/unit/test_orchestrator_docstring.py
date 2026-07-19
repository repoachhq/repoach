"""Pin the orchestrator module docstring to the evidence-first pipeline (SP-ORCH-DOCSTRING).

These tests guard against the retired verdict-first narrative silently
returning to ``src/repoach/review/orchestrator.py``.  They only inspect
the module docstring and the source file's text — no executable line
of the orchestrator is touched.
"""

from __future__ import annotations

from pathlib import Path

import repoach.review.orchestrator as orchestrator

_ORCHESTRATOR_PATH = Path(orchestrator.__file__).resolve()
_RETIRED_SUBSTRINGS = (
    "Aggregates their verdicts",
    "does **not** auto-merge",
    "run_coder_response",
)


def test_orchestrator_docstring_drops_the_retired_narrative() -> None:
    """The retired verdict-first phrases must not appear in the source file."""
    source = _ORCHESTRATOR_PATH.read_text(encoding="utf-8")
    for needle in _RETIRED_SUBSTRINGS:
        assert needle not in source, (
            f"orchestrator.py still contains the retired phrase {needle!r}; "
            "the evidence-first docstring has regressed."
        )


def test_orchestrator_docstring_describes_the_ledger_pipeline() -> None:
    """The module docstring must describe the ledger-derived verdict and report-only archive."""
    doc = orchestrator.__doc__
    assert isinstance(doc, str), "orchestrator module must expose a __doc__ string"
    for needle in ("findings", "derived", "report-only"):
        assert needle in doc, (
            f"orchestrator.__doc__ is missing the required token {needle!r}; "
            "the evidence-first pipeline description is incomplete."
        )
