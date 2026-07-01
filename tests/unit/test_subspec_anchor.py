"""Unit tests for SP-DEVAGENT-REVIEW-ANCHOR — sub-spec discovery + anchoring.

Pins: numeric-ordered discovery of ``<PARENT>-<N>`` sub-specs (ignoring unrelated and
non-numeric ids); the anchoring gate (parent still loads → None; no sub-specs → None;
parent gone + sub-specs → an AnchoredReview carrying both markdowns + each sub-spec's
arch edges); and never-raise on a malformed corpus.
"""

from __future__ import annotations

from pathlib import Path

from ferova.review.subspec_anchor import (
    AnchoredReview,
    discover_sub_specs,
    maybe_anchor_decomposed_parent,
    render_anchored_review_inputs,
)


def _write_spec(
    specs_dir: Path,
    spec_id: str,
    *,
    owns_code: tuple[str, ...] = (),
    depends_on: tuple[str, ...] = (),
    title: str | None = None,
) -> None:
    title = title or f"{spec_id} demo"
    code_lines = "\n".join(f"    - {p}" for p in owns_code) or "    []"
    content = (
        f"---\n"
        f"id: {spec_id}\n"
        f"title: {title}\n"
        f"version: 0.1\n"
        f"status: draft\n"
        f"owns:\n"
        f"  code:\n{code_lines}\n"
        f"  resources: []\n"
        f"depends_on: {list(depends_on)}\n"
        f"---\n\n"
        f"# {title}\n\nBody of {spec_id}.\n"
    )
    (specs_dir / f"2026-06-28_{spec_id}_x.md").write_text(content, encoding="utf-8")


def _specs_dir(tmp_path: Path) -> Path:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    return specs_dir


def test_discover_enumerates_numeric_sorted_and_ignores_unrelated(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    _write_spec(specs_dir, "SP-PARENT-1", owns_code=("src/a.py",))
    _write_spec(specs_dir, "SP-PARENT-2", owns_code=("src/b.py",))
    _write_spec(specs_dir, "SP-PARENT-10", owns_code=("src/c.py",))
    _write_spec(specs_dir, "SP-PARENT-FOO", owns_code=("src/d.py",))
    _write_spec(specs_dir, "SP-PARENTX", owns_code=("src/e.py",))

    found = discover_sub_specs("SP-PARENT", root=tmp_path)

    assert [s.id for s in found] == ["SP-PARENT-1", "SP-PARENT-2", "SP-PARENT-10"]


def test_anchor_returns_none_when_parent_still_loads(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    _write_spec(specs_dir, "SP-PARENT", owns_code=("src/p.py",))
    _write_spec(specs_dir, "SP-PARENT-1", owns_code=("src/a.py",))

    assert maybe_anchor_decomposed_parent("feat/sp-parent-impl", root=tmp_path) is None


def test_anchor_returns_none_when_no_sub_specs(tmp_path: Path) -> None:
    _specs_dir(tmp_path)

    assert maybe_anchor_decomposed_parent("feat/sp-parent-impl", root=tmp_path) is None


def test_anchor_returns_inputs_when_parent_gone_but_sub_specs_present(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    _write_spec(specs_dir, "SP-PARENT-1", owns_code=("src/a.py",), depends_on=("SP-ARCH-GRAPH",))
    _write_spec(specs_dir, "SP-PARENT-2", owns_code=("src/b.py",), depends_on=("SP-PARENT-1",))

    anchored = maybe_anchor_decomposed_parent("feat/sp-parent-impl", root=tmp_path)

    assert isinstance(anchored, AnchoredReview)
    assert anchored.parent_id == "SP-PARENT"
    assert anchored.sub_spec_ids == ("SP-PARENT-1", "SP-PARENT-2")
    assert "Sub-spec SP-PARENT-1" in anchored.spec_plan_md
    assert "Sub-spec SP-PARENT-2" in anchored.spec_plan_md
    assert "DECOMPOSED parent spec" in anchored.spec_plan_md
    assert "SP-ARCH-GRAPH" in anchored.arch_edges
    assert "SP-PARENT-1" in anchored.arch_edges


def test_anchor_multi_segment_parent_id_not_truncated(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    _write_spec(specs_dir, "SP-A-B-C-1", owns_code=("src/a.py",))
    _write_spec(specs_dir, "SP-A-B-C-2", owns_code=("src/b.py",))

    anchored = maybe_anchor_decomposed_parent("feat/sp-a-b-c-impl", root=tmp_path)

    assert anchored is not None
    assert anchored.parent_id == "SP-A-B-C"
    assert anchored.sub_spec_ids == ("SP-A-B-C-1", "SP-A-B-C-2")


def test_anchor_returns_none_for_non_spec_branch(tmp_path: Path) -> None:
    _specs_dir(tmp_path)

    assert maybe_anchor_decomposed_parent("main", root=tmp_path) is None
    assert maybe_anchor_decomposed_parent("", root=tmp_path) is None


def test_discover_never_raises_on_malformed_corpus(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    (specs_dir / "2026-06-28_SP-PARENT-1_x.md").write_text(
        "---\nid: SP-PARENT-1\n  bad: : yaml:\n---\n", encoding="utf-8"
    )

    assert discover_sub_specs("SP-PARENT", root=tmp_path) == []
    assert maybe_anchor_decomposed_parent("feat/sp-parent-impl", root=tmp_path) is None


def test_render_inputs_concatenates_markdown_and_edges(tmp_path: Path) -> None:
    specs_dir = _specs_dir(tmp_path)
    _write_spec(specs_dir, "SP-PARENT-1", owns_code=("src/a.py",), depends_on=("SP-ARCH-GRAPH",))
    _write_spec(specs_dir, "SP-PARENT-2", owns_code=("src/b.py",))
    subs = discover_sub_specs("SP-PARENT", root=tmp_path)

    markdown, edges = render_anchored_review_inputs(subs, root=tmp_path)

    assert markdown.count("## Sub-spec ") == 2
    assert "SP-ARCH-GRAPH" in edges
