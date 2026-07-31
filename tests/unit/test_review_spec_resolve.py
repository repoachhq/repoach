"""SP-CONSISTENCY-SWEEP G3 — ``_resolve_plan_file`` matches exact ids only.

Audit 2026-07-13 finding C3: ``_resolve_plan_file`` used
``base.glob(f"*{spec_id}*.md")`` — a substring glob, so
``spec_id="SP-SEC"`` also matched a sibling like
``..._SP-SECURITY-FOO_....md``, and the lexicographically-last match
silently won. The fix tightens resolution to the exact ``_(SP-…)_``
boundary extraction :func:`_scan_known_spec_ids` already uses: a file
matches only when its extracted id equals the requested id exactly.
"""

from __future__ import annotations

from pathlib import Path

from repoach.review.spec import _resolve_plan_file, load_spec


def _seed(base: Path, name: str, body: str = "body") -> Path:
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    path.write_text(body, encoding="utf-8")
    return path


def test_exact_id_beats_sibling(tmp_path: Path) -> None:
    """A substring sibling never wins over the exact-id spec.

    Pre-fix, ``base.glob(f"*SP-SEC*.md")`` matched BOTH files below
    (``SP-SECURITY-FOO`` contains ``SP-SEC`` as a substring), and the
    lexicographically-last match — ``SP-SECURITY-FOO`` sorts after
    ``SP-SEC`` — silently won, loading the wrong spec. This test fails
    on that pre-change code (it would return the security-foo file).
    """
    base = tmp_path / "docs" / "specs"
    sec_file = _seed(base, "2026-07-13_SP-SEC_hardening.md", "# SP-SEC\n")
    _seed(base, "2026-07-14_SP-SECURITY-FOO_extra.md", "# SP-SECURITY-FOO\n")

    resolved = _resolve_plan_file("SP-SEC", root=tmp_path)

    assert resolved == sec_file
    assert _resolve_plan_file("SP-MISSING", root=tmp_path) is None


def test_no_id_segment_never_false_matches(tmp_path: Path) -> None:
    """A filename with no ``_SP-…_`` segment is skipped, never a false match."""
    base = tmp_path / "docs" / "specs"
    _seed(base, "random-notes.md", "not a spec")

    assert _resolve_plan_file("SP-SEC", root=tmp_path) is None


def test_multiple_exact_matches_pick_lexicographic_last(tmp_path: Path) -> None:
    """Two exact-id matches (e.g. follow-up plans) → most recent wins."""
    base = tmp_path / "docs" / "specs"
    _seed(base, "2026-01-01_SP-SEC_v1.md", "# v1\n")
    newest = _seed(base, "2026-07-13_SP-SEC_v2.md", "# v2\n")

    assert _resolve_plan_file("SP-SEC", root=tmp_path) == newest


def test_load_spec_end_to_end_loads_exact_id_not_sibling(tmp_path: Path) -> None:
    """AC5(a) — real ``load_spec`` end to end through ``_resolve_plan_file``.

    Truthful boundary fake: real files on a real tmp specs dir, seeded
    with a sibling ``SP-SECURITY-*`` spec, driven through the real
    entrypoint rather than the internal helper in isolation.
    """
    base = tmp_path / "docs" / "specs"
    _seed(base, "2026-07-13_SP-SEC_hardening.md", "# SP-SEC\n\nThe real spec body.\n")
    _seed(base, "2026-07-14_SP-SECURITY-FOO_extra.md", "# SP-SECURITY-FOO\n\nsibling.\n")

    plan = load_spec("SP-SEC", root=tmp_path)

    assert plan.id == "SP-SEC"
    assert "SP-SECURITY-FOO" not in plan.raw_markdown
