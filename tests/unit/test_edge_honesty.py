"""Tests for the edge-honesty gate (SP-ARCH-EDGE-GATE).

Covers tier-1a import enforcement, tier-1b table back-channel, frontier
non-blocking + aggregation + suppression, the direct-import (not
transitive) rule, tier-2 non-enforcement, and self-application on the real
``edge_honesty.py`` importing the governed ``arch`` package.
"""

from __future__ import annotations

from pathlib import Path

from ferova.arch import load_registry
from ferova.lint.edge_honesty import check_diff, load_frontier_suppress, report_lines

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _spec(
    owner_id: str, *, owns_code: str = "", owns_resources: str = "", depends_on: str = ""
) -> str:
    return (
        "---\n"
        f"id: {owner_id}\n"
        f"title: {owner_id}\n"
        "version: 0.1\n"
        "status: approved\n"
        "owns:\n"
        f"  code: [{owns_code}]\n"
        f"  resources: [{owns_resources}]\n"
        f"depends_on: [{depends_on}]\n"
        "---\n\n# body\n"
    )


def _corpus(tmp_path: Path, specs: dict[str, str]) -> Path:
    specs_dir = tmp_path / "docs" / "specs"
    specs_dir.mkdir(parents=True)
    for name, text in specs.items():
        (specs_dir / name).write_text(text, encoding="utf-8")
    return specs_dir


def _src(tmp_path: Path, rel: str, body: str) -> str:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return rel


def test_tier1a_undeclared_import_is_a_violation(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"'),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/ferova/b/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/a/mod.py", "from ferova.b.thing import X\n")

    report = check_diff(registry, [changed], tmp_path)

    assert not report.ok
    assert report.violations[0].source == "SP-A"
    assert report.violations[0].target == "SP-B"
    assert report.violations[0].kind == "import"


def test_tier1a_declared_import_passes(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"', depends_on="SP-B"),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/ferova/b/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/a/mod.py", "from ferova.b.thing import X\n")

    assert check_diff(registry, [changed], tmp_path).ok


def test_tier1b_table_backchannel(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"'),
            "01_SP-DB_db.md": _spec(
                "SP-DB", owns_code="src/ferova/db/", owns_resources='"db:table:orders"'
            ),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/a/mod.py", 'Table("orders", meta)\n')

    report = check_diff(registry, [changed], tmp_path)
    assert not report.ok
    assert report.violations[0].kind == "table"
    assert report.violations[0].target == "SP-DB"


def test_tier1b_literal_in_owner_is_fine(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-DB_db.md": _spec(
                "SP-DB", owns_code="src/ferova/db/", owns_resources='"db:table:orders"'
            ),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/db/store.py", 'Table("orders", meta)\n')

    assert check_diff(registry, [changed], tmp_path).ok


def test_frontier_is_non_blocking_and_aggregated(tmp_path: Path) -> None:
    specs = _corpus(tmp_path, {"01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"')})
    registry = load_registry(specs)
    changed = _src(
        tmp_path,
        "src/ferova/a/mod.py",
        'Table("legacy_one", m)\nTable("legacy_two", m)\nTable("legacy_three", m)\n',
    )

    report = check_diff(registry, [changed], tmp_path)
    assert report.ok
    assert len(report.frontier) == 3
    frontier_lines = [line for line in report_lines(report) if line.startswith("frontier:")]
    assert len(frontier_lines) == 1


def test_frontier_suppression(tmp_path: Path) -> None:
    specs = _corpus(tmp_path, {"01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"')})
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/a/mod.py", 'Table("legacy_one", m)\n')

    suppress = frozenset({"db:table:legacy_one"})
    report = check_diff(registry, [changed], tmp_path, suppress=suppress)
    assert report.ok
    assert report.frontier == ()


def test_direct_import_not_transitive(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"', depends_on="SP-B"),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/ferova/b/"),
            "01_SP-C_c.md": _spec("SP-C", owns_code="src/ferova/c/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/ferova/a/mod.py", "from ferova.b.reexport import X\n")

    assert check_diff(registry, [changed], tmp_path).ok


def test_tier2_runtime_topic_and_non_literal_table_not_enforced(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/ferova/a/mod.py"'),
            "01_SP-DB_db.md": _spec("SP-DB", owns_resources='"db:table:orders"'),
        },
    )
    registry = load_registry(specs)
    changed = _src(
        tmp_path,
        "src/ferova/a/mod.py",
        'name = "orders"\nTable(name, meta)\npublish("queue:topic:" + name)\n',
    )

    assert check_diff(registry, [changed], tmp_path).ok


def test_self_application_on_real_gate_passes() -> None:
    registry = load_registry(_REPO_ROOT / "docs" / "specs")
    suppress = load_frontier_suppress(_REPO_ROOT)
    report = check_diff(
        registry, ["src/ferova/lint/edge_honesty.py"], _REPO_ROOT, suppress=suppress
    )
    assert report.ok
