"""Pin the selfverify judge persona's evidence contract (SP-SELFVERIFY-REFUTABLE-GAPS).

The 0.2.0 persona must carry the absence-claim evidence contract the
mechanical refutation pass in :mod:`repoach.review.devagent_selfverify`
relies on: gap objects with ``absent_pattern`` evidence, illustrated by
an example object literal the judge can imitate.
"""

from __future__ import annotations

from pathlib import Path

_PERSONA_PATH = Path("prompts/review/judge_selfverify_0.2.0.md")


def test_persona_0_2_0_has_evidence_contract() -> None:
    """The persona names the evidence fields and shows an example gap object."""
    text = _PERSONA_PATH.read_text(encoding="utf-8")
    assert "absent_pattern" in text
    assert '"claim"' in text
    assert '"file"' in text
    assert "Evidence contract for absence claims" in text
