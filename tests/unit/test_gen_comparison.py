"""``scripts/gen_comparison.py``: the comparison page, its sources and their age.

SP-COMPARISON-PAGE AC1-AC5, with the network replaced by a fake transport.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest
import yaml

_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
_REPO_YAML = _SCRIPTS.parent / "docs" / "comparison.yaml"
_REPO_PAGE = _SCRIPTS.parent / "docs" / "comparison.md"
_TODAY = date(2026, 10, 21)


def _load() -> ModuleType:
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    spec = importlib.util.spec_from_file_location("gen_comparison", _SCRIPTS / "gen_comparison.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_comparison"] = module
    spec.loader.exec_module(module)
    return module


gc = _load()
Probe = sys.modules["check_links"].Probe


def _all_up(url: str, user_agent: str) -> object:
    return Probe(status=200)


def _down_for(*dead: str):
    def fetch(url: str, user_agent: str) -> object:
        if url in dead:
            return Probe(status=None, resolved=False)
        return Probe(status=200)

    return fetch


def _no_network(url: str, user_agent: str) -> object:
    return Probe(status=None, resolved=False)


def _cell(value: str, url: str = "https://src.example/page", day: str = "2026-10-21") -> dict:
    return {"value": value, "source_url": url, "checked_on": day}


def _product(name: str, **overrides: dict) -> dict:
    product = {"name": name, "vendor_url": f"https://{name.lower()}.example/"}
    for key in gc.COLUMNS:
        product[key] = overrides.get(key, {"value": "n/m"})
    return product


def _write_yaml(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "comparison.yaml"
    path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
    return path


def _run(tmp_path: Path, yaml_path: Path, *flags: str, fetch=_all_up) -> int:
    out = tmp_path / "comparison.md"
    return gc.main(["--yaml", str(yaml_path), "--out", str(out), *flags], fetch=fetch, today=_TODAY)


def test_committed_page_is_the_rendered_page() -> None:
    comparison = gc.load(_REPO_YAML)

    assert gc.render(comparison) == _REPO_PAGE.read_text(encoding="utf-8")


def test_committed_page_has_eleven_rows_and_pr_sources_apart() -> None:
    page = _REPO_PAGE.read_text(encoding="utf-8")
    table = [line for line in page.splitlines() if line.startswith("| ")]

    assert len(table) == 13
    before, _, after = page.partition("## PR sources, not competitors")
    assert "openhands" not in before.lower()
    assert "openhands" in after.lower()


def test_repoach_row_is_sourced_from_this_repository_at_a_commit() -> None:
    repoach = gc.load(_REPO_YAML).products[0]

    assert repoach.name == "repoach"
    for key in gc.COLUMNS:
        url = repoach.cell(key).source_url
        assert url is not None
        assert url.startswith("https://github.com/repoachhq/repoach/")
        assert len(url.split("/")[6]) == 40


def test_a_dead_source_names_its_cell_and_offline_still_renders(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    document = yaml.safe_load(_REPO_YAML.read_text(encoding="utf-8"))
    coderabbit = next(p for p in document["products"] if p["name"] == "CodeRabbit")
    coderabbit["price"]["source_url"] = "https://example.invalid/x"
    yaml_path = _write_yaml(tmp_path, document)
    fetch = _down_for("https://example.invalid/x")

    assert _run(tmp_path, yaml_path, "--offline", fetch=fetch) == 0
    assert "https://example.invalid/x" in (tmp_path / "comparison.md").read_text(encoding="utf-8")

    assert _run(tmp_path, yaml_path, fetch=fetch) == 1
    out = capsys.readouterr().out.splitlines()
    assert "dead: https://example.invalid/x (comparison.yaml: CodeRabbit.price)" in out


def test_a_stale_reading_exits_three(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    document = {
        "products": [
            _product("Old", price=_cell("$1", day="2020-01-01")),
            _product("Fresh", price=_cell("$2")),
        ]
    }
    yaml_path = _write_yaml(tmp_path, document)

    assert _run(tmp_path, yaml_path) == 3
    assert "stale: 1 cell older than 90 days" in capsys.readouterr().out.splitlines()
    assert not (tmp_path / "comparison.md").exists()


def test_not_measured_cells_carry_no_footnote_and_are_never_stale(tmp_path: Path) -> None:
    document = {"products": [_product("Only", loop=_cell("review comments"))]}
    yaml_path = _write_yaml(tmp_path, document)

    assert _run(tmp_path, yaml_path) == 0
    page = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    row = next(line for line in page.splitlines() if line.startswith("| [Only]"))
    assert row.count("[^") == 1
    assert row.count("| n/m") == len(gc.COLUMNS) - 1
    assert "(oldest cell checked on 2026-10-21)" in page.splitlines()[0]


def test_a_sourced_cell_without_a_date_is_invalid(tmp_path: Path) -> None:
    document = {
        "products": [_product("Bad", price={"value": "$1", "source_url": "https://x.example/"})]
    }
    yaml_path = _write_yaml(tmp_path, document)

    assert _run(tmp_path, yaml_path, "--offline") == 2


def test_no_network_is_cannot_say_and_leaves_the_page(tmp_path: Path) -> None:
    yaml_path = _write_yaml(tmp_path, {"products": [_product("Only", loop=_cell("x"))]})
    out = tmp_path / "comparison.md"
    out.write_text("previous\n", encoding="utf-8")

    assert _run(tmp_path, yaml_path, fetch=_no_network) == 2
    assert out.read_text(encoding="utf-8") == "previous\n"


def test_check_only_writes_nothing(tmp_path: Path) -> None:
    yaml_path = _write_yaml(tmp_path, {"products": [_product("Only", loop=_cell("x"))]})

    assert _run(tmp_path, yaml_path, "--check-only") == 0
    assert not (tmp_path / "comparison.md").exists()


def test_retired_products_are_listed_with_their_last_reading(tmp_path: Path) -> None:
    document = {
        "products": [_product("Only", loop=_cell("x"))],
        "retired": [{"name": "Gone", "last_checked_on": "2026-01-02"}],
    }
    yaml_path = _write_yaml(tmp_path, document)

    assert _run(tmp_path, yaml_path, "--offline") == 0
    page = (tmp_path / "comparison.md").read_text(encoding="utf-8")
    assert "- **Gone**, last checked on 2026-01-02" in page
