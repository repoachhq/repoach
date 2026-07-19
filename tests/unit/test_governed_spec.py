"""Tests for the review-side governed-spec accessor (SP-ARCH-REVIEW-WIRE).

Covers loading declared owns/depends_on for a governed spec, the frontier
(no-frontmatter) → None fallback, the rendered {ARCH_EDGES} block, and the
malformed-frontmatter propagation.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.arch import MalformedFrontmatterError
from repoach.review.governed_spec import (
    GovernedSpec,
    load_governed_spec,
    render_arch_edges,
)


def _governed(spec_id: str, *, depends_on: str = "", owns_code: str = "src/x/") -> str:
    return (
        "---\n"
        f"id: {spec_id}\n"
        f"title: {spec_id}\n"
        "version: 0.1\n"
        "status: approved\n"
        "owns:\n"
        f"  code: [{owns_code}]\n"
        "  resources: N/A\n"
        f"depends_on: [{depends_on}]\n"
        "---\n\n# body\n"
    )


def _corpus(tmp_path: Path, specs: dict[str, str]) -> None:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    for name, text in specs.items():
        (specs_dir / name).write_text(text, encoding="utf-8")


def test_load_governed_spec_returns_declared_edges(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        {
            "01_SP-WIRE_w.md": _governed("SP-WIRE", depends_on="SP-ARCH-GRAPH"),
            "01_SP-ARCH-GRAPH_g.md": _governed("SP-ARCH-GRAPH", owns_code="src/arch/"),
        },
    )
    spec = load_governed_spec("SP-WIRE", root=tmp_path)
    assert isinstance(spec, GovernedSpec)
    assert spec.id == "SP-WIRE"
    assert spec.depends_on == ("SP-ARCH-GRAPH",)


def test_frontier_spec_resolves_to_none(tmp_path: Path) -> None:
    _corpus(tmp_path, {"01_SP-LEGACY_l.md": "# legacy, no frontmatter\n\ntext\n"})
    assert load_governed_spec("SP-LEGACY", root=tmp_path) is None


def test_unknown_spec_resolves_to_none(tmp_path: Path) -> None:
    _corpus(tmp_path, {"01_SP-A_a.md": _governed("SP-A")})
    assert load_governed_spec("SP-DOES-NOT-EXIST", root=tmp_path) is None


def test_render_arch_edges_names_dependencies(tmp_path: Path) -> None:
    _corpus(
        tmp_path,
        {
            "01_SP-WIRE_w.md": _governed("SP-WIRE", depends_on="SP-ARCH-GRAPH"),
            "01_SP-ARCH-GRAPH_g.md": _governed("SP-ARCH-GRAPH", owns_code="src/arch/"),
        },
    )
    block = render_arch_edges(load_governed_spec("SP-WIRE", root=tmp_path))
    assert "SP-ARCH-GRAPH" in block
    assert "tier-2" in block.lower()


def test_render_arch_edges_empty_for_none() -> None:
    assert render_arch_edges(None) == ""


def test_render_arch_edges_no_deps_flags_any_coupling(tmp_path: Path) -> None:
    _corpus(tmp_path, {"01_SP-ROOT_r.md": _governed("SP-ROOT")})
    block = render_arch_edges(load_governed_spec("SP-ROOT", root=tmp_path))
    assert "NO dependencies" in block


def test_malformed_frontmatter_propagates(tmp_path: Path) -> None:
    _corpus(tmp_path, {"01_SP-BAD_b.md": "---\nid: SP-BAD\nowns: not-a-mapping\n---\n"})
    with pytest.raises(MalformedFrontmatterError):
        load_governed_spec("SP-BAD", root=tmp_path)
