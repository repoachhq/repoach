"""Tests for the edge-honesty gate (SP-ARCH-EDGE-GATE).

Covers tier-1a import enforcement, tier-1b table back-channel, frontier
non-blocking + aggregation + suppression, the direct-import (not
transitive) rule, tier-2 non-enforcement, and self-application on the real
``edge_honesty.py`` importing the governed ``arch`` package.
"""

from __future__ import annotations

import subprocess
import tomllib
from pathlib import Path

import pytest

from repoach.arch import load_registry
from repoach.lint import edge_honesty
from repoach.lint.edge_honesty import (
    _read_source,
    check_diff,
    gather_added_specs,
    gather_changed_files,
    load_frontier_suppress,
    report_lines,
    run,
)

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


def _git(repo_root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo_root, check=True, capture_output=True, text=True)


def _init_repo(repo_root: Path) -> None:
    _git(repo_root, "init", "-q")
    _git(repo_root, "config", "user.email", "test@example.com")
    _git(repo_root, "config", "user.name", "Test")


def test_tier1a_undeclared_import_is_a_violation(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"'),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/repoach/b/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/a/mod.py", "from repoach.b.thing import X\n")

    report = check_diff(registry, [changed], tmp_path)

    assert not report.ok
    assert report.violations[0].source == "SP-A"
    assert report.violations[0].target == "SP-B"
    assert report.violations[0].kind == "import"


def test_tier1a_declared_import_passes(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"', depends_on="SP-B"),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/repoach/b/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/a/mod.py", "from repoach.b.thing import X\n")

    assert check_diff(registry, [changed], tmp_path).ok


def test_tier1b_table_backchannel(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"'),
            "01_SP-DB_db.md": _spec(
                "SP-DB", owns_code="src/repoach/db/", owns_resources='"db:table:orders"'
            ),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/a/mod.py", 'Table("orders", meta)\n')

    report = check_diff(registry, [changed], tmp_path)
    assert not report.ok
    assert report.violations[0].kind == "table"
    assert report.violations[0].target == "SP-DB"


def test_tier1b_literal_in_owner_is_fine(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-DB_db.md": _spec(
                "SP-DB", owns_code="src/repoach/db/", owns_resources='"db:table:orders"'
            ),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/db/store.py", 'Table("orders", meta)\n')

    assert check_diff(registry, [changed], tmp_path).ok


def test_frontier_is_non_blocking_and_aggregated(tmp_path: Path) -> None:
    specs = _corpus(tmp_path, {"01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"')})
    registry = load_registry(specs)
    changed = _src(
        tmp_path,
        "src/repoach/a/mod.py",
        'Table("legacy_one", m)\nTable("legacy_two", m)\nTable("legacy_three", m)\n',
    )

    report = check_diff(registry, [changed], tmp_path)
    assert report.ok
    assert len(report.frontier) == 3
    frontier_lines = [line for line in report_lines(report) if line.startswith("frontier:")]
    assert len(frontier_lines) == 1


def test_frontier_suppression(tmp_path: Path) -> None:
    specs = _corpus(tmp_path, {"01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"')})
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/a/mod.py", 'Table("legacy_one", m)\n')

    suppress = frozenset({"db:table:legacy_one"})
    report = check_diff(registry, [changed], tmp_path, suppress=suppress)
    assert report.ok
    assert report.frontier == ()


def test_direct_import_not_transitive(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"', depends_on="SP-B"),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/repoach/b/"),
            "01_SP-C_c.md": _spec("SP-C", owns_code="src/repoach/c/"),
        },
    )
    registry = load_registry(specs)
    changed = _src(tmp_path, "src/repoach/a/mod.py", "from repoach.b.reexport import X\n")

    assert check_diff(registry, [changed], tmp_path).ok


def test_tier2_runtime_topic_and_non_literal_table_not_enforced(tmp_path: Path) -> None:
    specs = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"'),
            "01_SP-DB_db.md": _spec("SP-DB", owns_resources='"db:table:orders"'),
        },
    )
    registry = load_registry(specs)
    changed = _src(
        tmp_path,
        "src/repoach/a/mod.py",
        'name = "orders"\nTable(name, meta)\npublish("queue:topic:" + name)\n',
    )

    assert check_diff(registry, [changed], tmp_path).ok


def test_self_application_on_real_gate_passes() -> None:
    registry = load_registry(_REPO_ROOT / "docs" / "specs")
    suppress = load_frontier_suppress(_REPO_ROOT)
    report = check_diff(
        registry, ["src/repoach/lint/edge_honesty.py"], _REPO_ROOT, suppress=suppress
    )
    assert report.ok


def test_staged_mode_reads_staged_blob_not_worktree(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    rel = _src(tmp_path, "mod.py", "staged content\n")
    _git(tmp_path, "add", rel)
    (tmp_path / rel).write_text("worktree content\n", encoding="utf-8")

    assert _read_source(rel, staged=True, repo_root=tmp_path) == "staged content\n"
    assert _read_source(rel, staged=False, repo_root=tmp_path) == "worktree content\n"


def test_staged_reverted_worktree_still_flagged(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    specs_dir = _corpus(
        tmp_path,
        {
            "01_SP-A_a.md": _spec("SP-A", owns_code='"src/repoach/a/mod.py"'),
            "01_SP-B_b.md": _spec("SP-B", owns_code="src/repoach/b/"),
        },
    )
    rel = _src(tmp_path, "src/repoach/a/mod.py", "from repoach.b.thing import X\n")
    _git(tmp_path, "add", rel)
    (tmp_path / rel).write_text("x = 1\n", encoding="utf-8")

    staged_report = run(base="develop", staged=True, specs_dir=specs_dir, repo_root=tmp_path)
    assert not staged_report.ok
    assert staged_report.violations[0].source == "SP-A"
    assert staged_report.violations[0].target == "SP-B"
    assert staged_report.violations[0].kind == "import"

    registry = load_registry(specs_dir)
    worktree_report = check_diff(registry, [rel], tmp_path, staged=False)
    assert worktree_report.ok


def test_git_argv_has_double_dash_separator(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    (tmp_path / "f.txt").write_text("one\n", encoding="utf-8")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "init")
    _git(tmp_path, "checkout", "-q", "-b", "develop")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    _git(tmp_path, "add", "f.txt")
    _git(tmp_path, "commit", "-q", "-m", "second")

    captured: list[list[str]] = []
    real_run = subprocess.run

    def _spy(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured.append(command)
        return real_run(command, **kwargs)

    monkeypatch.setattr(edge_honesty.subprocess, "run", _spy)

    changed = gather_changed_files(base="develop", staged=False, repo_root=tmp_path)
    added = gather_added_specs(base="develop", staged=False, repo_root=tmp_path)

    assert changed == ["f.txt"]
    assert added == []
    assert len(captured) == 2
    for command in captured:
        assert command[-2] == "develop...HEAD"
        assert command[-1] == "--"


def test_frontier_suppress_wired() -> None:
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    arch = data.get("tool", {}).get("repoach", {}).get("arch", {})

    assert "frontier_suppress" in arch
    assert load_frontier_suppress(_REPO_ROOT) == frozenset(
        str(item) for item in arch["frontier_suppress"]
    )
