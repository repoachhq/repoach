"""Tests for the architecture dependency-graph deriver (SP-ARCH-GRAPH).

Covers frontmatter parsing into a registry (incl. frontier tolerance),
owner resolution for code paths and resource keys, disjointness, cycle
detection, rendering, and a full run over the real ``docs/specs/``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repoach.arch import (
    Graph,
    MalformedFrontmatterError,
    OwnershipConflictError,
    load_registry,
    render,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec(
    owner_id: str, *, owns_code: str = "", owns_resources: str = "", depends_on: str = ""
) -> str:
    return (
        "---\n"
        f"id: {owner_id}\n"
        f"title: {owner_id} title\n"
        "version: 0.1\n"
        "status: approved\n"
        "owns:\n"
        f"  code: [{owns_code}]\n"
        f"  resources: [{owns_resources}]\n"
        f"depends_on: [{depends_on}]\n"
        "---\n\n# body\n"
    )


def _write(spec_dir: Path, name: str, text: str) -> None:
    (spec_dir / name).write_text(text, encoding="utf-8")


def test_load_registry_parses_and_tolerates_frontier(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-A_a.md", _spec("SP-A", owns_code="src/a/"))
    _write(tmp_path, "2026-01-01_SP-LEGACY_old.md", "# legacy spec, no frontmatter\n\ntext\n")
    _write(tmp_path, "_TEMPLATE.md", _spec("SP-IGNORED", owns_code="src/x/"))

    registry = load_registry(tmp_path)

    assert set(registry.nodes) == {"SP-A", "SP-LEGACY"}
    assert registry.nodes["SP-A"].owns_code == ("src/a/",)
    assert registry.nodes["SP-LEGACY"].frontier is True
    assert registry.frontier_ids() == ("SP-LEGACY",)


def test_owner_of_resolves_code_path_and_table_resource(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-A_a.md", _spec("SP-A", owns_code="src/a/"))
    _write(
        tmp_path,
        "2026-01-01_SP-DB_db.md",
        _spec("SP-DB", owns_resources='"db:table:orders"'),
    )
    registry = load_registry(tmp_path)

    assert registry.owner_of("src/a/deep/module.py") == "SP-A"
    assert registry.owner_of("db:table:orders") == "SP-DB"
    assert registry.owner_of("src/elsewhere/x.py") is None
    assert registry.owner_of("db:table:unknown") is None


def test_longest_prefix_wins(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-PKG_pkg.md", _spec("SP-PKG", owns_code="src/pkg/"))
    _write(tmp_path, "2026-01-01_SP-SUB_sub.md", _spec("SP-SUB", owns_code='"src/pkg/sub/x.py"'))
    registry = load_registry(tmp_path)

    assert registry.owner_of("src/pkg/sub/x.py") == "SP-SUB"
    assert registry.owner_of("src/pkg/other.py") == "SP-PKG"


def test_disjointness_violation_names_both_specs(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-A_a.md", _spec("SP-A", owns_code="src/shared/"))
    _write(tmp_path, "2026-01-01_SP-B_b.md", _spec("SP-B", owns_code='"src/shared/mod.py"'))
    registry = load_registry(tmp_path)

    conflicts = registry.disjointness_violations()
    assert len(conflicts) == 1
    assert conflicts[0].spec_ids == ("SP-A", "SP-B")
    with pytest.raises(OwnershipConflictError):
        registry.assert_disjoint()


def test_duplicate_id_is_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-A_one.md", _spec("SP-A", owns_code="src/one/"))
    _write(tmp_path, "2026-01-02_SP-A_two.md", _spec("SP-A", owns_code="src/two/"))
    with pytest.raises(MalformedFrontmatterError):
        load_registry(tmp_path)


def test_malformed_frontmatter_raises(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-BAD_bad.md", "---\nid: SP-BAD\nowns: not-a-mapping\n---\n")
    with pytest.raises(MalformedFrontmatterError):
        load_registry(tmp_path)


def test_cycle_detection() -> None:
    graph = Graph(edges={"A": frozenset({"B"}), "B": frozenset({"C"}), "C": frozenset({"A"})})
    cycles = graph.cycles()
    assert cycles == [["A", "B", "C"]]


def test_dangling_edge_is_not_a_cycle_and_is_reported(tmp_path: Path) -> None:
    _write(
        tmp_path, "2026-01-01_SP-A_a.md", _spec("SP-A", owns_code="src/a/", depends_on="SP-GHOST")
    )
    registry = load_registry(tmp_path)

    assert registry.graph().cycles() == []
    assert registry.dangling_edges() == [("SP-A", "SP-GHOST")]


def test_render_mermaid_and_json(tmp_path: Path) -> None:
    _write(tmp_path, "2026-01-01_SP-A_a.md", _spec("SP-A", owns_code="src/a/", depends_on="SP-B"))
    _write(tmp_path, "2026-01-01_SP-B_b.md", _spec("SP-B", owns_code="src/b/"))
    registry = load_registry(tmp_path)

    mermaid = render(registry, "mermaid")
    assert mermaid.startswith("flowchart TD")
    assert "SP_A --> SP_B" in mermaid

    payload = json.loads(render(registry, "json"))
    assert {"from": "SP-A", "to": "SP-B"} in payload["edges"]
    assert payload["cycles"] == []

    with pytest.raises(ValueError):
        render(registry, "yaml")


def test_full_run_over_real_specs_does_not_crash() -> None:
    registry = load_registry(_REPO_ROOT / "docs" / "specs")
    assert "SP-SPEC-TEMPLATE" in registry.nodes
    assert registry.owner_of("format:spec-frontmatter") == "SP-SPEC-TEMPLATE"
    assert isinstance(render(registry, "json"), str)


def test_health_credits_ownership_is_disjoint() -> None:
    """Per-module ownership split: credits.py is SP-CREDITS-CHECK while
    store.py is SP-HEALTH-STORE-NEUTRALIZE, proving the narrowing holds."""
    registry = load_registry(_REPO_ROOT / "docs" / "specs")

    assert registry.owner_of("src/repoach/health/credits.py") == "SP-CREDITS-CHECK"
    assert registry.owner_of("src/repoach/health/store.py") == "SP-HEALTH-STORE-NEUTRALIZE"
