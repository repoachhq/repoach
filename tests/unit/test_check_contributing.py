"""``scripts/check_contributing.py``: paths CONTRIBUTING.md cites are judged against disk.

SP-CONTRIBUTING G3.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_contributing.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_contributing", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_contributing"] = module
    spec.loader.exec_module(module)
    return module


cc = _load()


def _repo(tmp_path: Path) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "ci_local.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "CONTRIBUTING.md").write_text("", encoding="utf-8")
    return tmp_path


def test_is_candidate_accepts_a_top_level_relative_path() -> None:
    entries = frozenset({"scripts", "CONTRIBUTING.md"})
    assert cc._is_candidate("scripts/ci_local.sh", entries)
    assert cc._is_candidate("CONTRIBUTING.md", entries)


def test_is_candidate_rejects_a_git_ref_and_a_flag() -> None:
    entries = frozenset({"scripts", "docs"})
    assert not cc._is_candidate("origin/develop", entries)
    assert not cc._is_candidate("feat/my-change", entries)
    assert not cc._is_candidate("--base", entries)
    assert not cc._is_candidate("https://example.com/docs", entries)


def test_is_candidate_rejects_a_template_placeholder() -> None:
    entries = frozenset({"docs"})
    assert not cc._is_candidate("docs/specs/<date>_<SP-ID>_<slug>.md", entries)
    assert not cc._is_candidate("docs/…", entries)


def test_extract_citations_reads_inline_code_and_fenced_blocks(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    entries = cc.top_level_entries(root)
    text = "Run `scripts/ci_local.sh` first.\n\n```bash\nscripts/ci_local.sh --fast\n```\n"
    path = root / "CONTRIBUTING.md"
    path.write_text(text, encoding="utf-8")
    citations = cc.extract_citations(path, entries)
    tokens = [c.token for c in citations]
    assert tokens.count("scripts/ci_local.sh") == 2


def test_missing_citations_flags_a_dead_path(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    path = root / "CONTRIBUTING.md"
    citations = [
        cc.Citation(file=path, line=1, token="scripts/ci_local.sh"),
        cc.Citation(file=path, line=2, token="scripts/does_not_exist.sh"),
    ]
    missing = cc.missing_citations(citations, root)
    assert [c.token for c in missing] == ["scripts/does_not_exist.sh"]


def test_main_exits_zero_when_every_citation_resolves(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    path = root / "CONTRIBUTING.md"
    path.write_text("Run `scripts/ci_local.sh --fast`.\n", encoding="utf-8")
    monkeypatch.setattr(cc, "repo_root", lambda: root)
    exit_code = cc.main([str(path)])
    out = capsys.readouterr().out
    assert exit_code == cc.EXIT_OK
    assert "0 missing" in out


def test_main_exits_one_and_names_a_dead_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path)
    path = root / "CONTRIBUTING.md"
    path.write_text("Run `scripts/ci_local_bogus.sh`.\n", encoding="utf-8")
    monkeypatch.setattr(cc, "repo_root", lambda: root)
    exit_code = cc.main([str(path)])
    out = capsys.readouterr().out
    assert exit_code == cc.EXIT_MISSING
    assert "scripts/ci_local_bogus.sh missing" in out
