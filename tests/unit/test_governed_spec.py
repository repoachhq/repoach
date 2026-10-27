"""Tests for the review-side governed-spec accessor (SP-ARCH-REVIEW-WIRE).

Covers loading declared owns/depends_on for a governed spec, the frontier
(no-frontmatter) → None fallback, the rendered {ARCH_EDGES} block, and the
malformed-frontmatter propagation.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from repoach.arch import MalformedFrontmatterError
from repoach.review.governed_spec import (
    GovernedSpec,
    SpecRefUnavailableError,
    governed_specs_at_ref,
    load_governed_spec,
    ref_resolves,
    render_arch_edges,
    spec_markdown_at_ref,
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


def _committed_corpus(tmp_path: Path, specs: dict[str, str]) -> Path:
    _corpus(tmp_path, specs)
    for args in (
        ("init", "-q", "-b", "develop"),
        ("config", "user.email", "test@example.invalid"),
        ("config", "user.name", "Test Runner"),
        ("add", "-A"),
        ("commit", "-q", "-m", "base"),
    ):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, text=True, check=True)
    return tmp_path


@pytest.fixture
def corpus_at_ref(tmp_path: Path) -> Path:
    return _committed_corpus(
        tmp_path,
        {
            "2026-10-27_SP-GOOD_good.md": _governed("SP-GOOD"),
            "2026-10-27_SP-FRONT_front.md": "# Frontier\n\nNo frontmatter.\n",
            "2026-10-27_SP-BAD_bad.md": "---\nid: SP-BAD\n---\n\n# Missing keys\n",
        },
    )


def test_governed_specs_at_ref_skip_frontier_and_malformed(corpus_at_ref: Path) -> None:
    specs = governed_specs_at_ref("develop", root=corpus_at_ref)
    assert [spec.id for spec in specs] == ["SP-GOOD"]


def test_spec_markdown_at_ref_reads_a_spec_and_misses_an_unknown_id(
    corpus_at_ref: Path,
) -> None:
    assert spec_markdown_at_ref("SP-GOOD", "develop", root=corpus_at_ref) == _governed("SP-GOOD")
    assert spec_markdown_at_ref("SP-NONE", "develop", root=corpus_at_ref) is None


def test_ref_resolves_only_a_commit(corpus_at_ref: Path) -> None:
    def rev(spec: str) -> str:
        return subprocess.run(
            ["git", "rev-parse", spec],
            cwd=corpus_at_ref,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()

    assert ref_resolves(corpus_at_ref, "develop") is True
    assert ref_resolves(corpus_at_ref, rev("develop")) is True
    assert ref_resolves(corpus_at_ref, "no-such-branch") is False
    assert ref_resolves(corpus_at_ref, rev("develop^{tree}")) is False
    assert (
        ref_resolves(corpus_at_ref, rev("develop:docs/specs/2026-10-27_SP-GOOD_good.md")) is False
    )


def test_readers_at_an_unresolvable_ref_raise(corpus_at_ref: Path) -> None:
    with pytest.raises(SpecRefUnavailableError):
        governed_specs_at_ref("no-such-branch", root=corpus_at_ref)
    with pytest.raises(SpecRefUnavailableError):
        spec_markdown_at_ref("SP-GOOD", "no-such-branch", root=corpus_at_ref)
