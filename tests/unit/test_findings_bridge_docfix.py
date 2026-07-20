"""Pin the docstring invariants of ``_files_in_diff`` (SP-FINDINGS-BRIDGE-DOCFIX).

The stale ``coder_loop._files_in_diff`` cross-reference and the
"temporary duplicate" framing were removed from the helper's docstring
because no such counterpart exists in the repository. These tests
guard against the stale prose silently returning and confirm the
failure-soft rationale is still documented.
"""

from __future__ import annotations

from repoach.review import findings_bridge


def test_no_coder_loop_reference() -> None:
    doc = findings_bridge._files_in_diff.__doc__
    assert isinstance(doc, str)
    assert "coder_loop" not in doc


def test_failure_soft_contract_documented() -> None:
    doc = findings_bridge._files_in_diff.__doc__
    assert isinstance(doc, str)
    lowered = doc.lower()
    assert "malformed" in lowered
    assert "empty" in lowered
    assert "keeps every comment" in lowered
