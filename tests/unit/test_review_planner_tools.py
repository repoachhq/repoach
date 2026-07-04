"""SP-PLANNER-AGENT — the Planner's repo-jailed exploration toolbox.

Pins the jail (lexical + resolved containment), the caps, the
error-string contract (tools never raise on bad model input) and the
ToolDef metadata the AgentLoop serialises for the model.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ferova.review.planner_tools import make_planner_tools


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "alpha.py").write_text(
        "def alpha() -> int:\n    return 1\n", encoding="utf-8"
    )
    (tmp_path / "src" / "pkg" / "beta.py").write_text(
        "def beta() -> int:\n    return 2\n", encoding="utf-8"
    )
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").write_text("alpha is documented\n", encoding="utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "secret_outside.txt").write_text("jail me\n", encoding="utf-8")
    return tmp_path


def _tools_by_name(repo_root: Path) -> dict:
    return {t.name: t for t in make_planner_tools(repo_root)}


class TestJail:
    def test_absolute_path_refused(self, repo: Path) -> None:
        out = _tools_by_name(repo)["read_file"].callable_fn(path="/etc/passwd")
        assert out.startswith("error:")
        assert "absolute" in out

    def test_traversal_refused(self, repo: Path) -> None:
        out = _tools_by_name(repo)["read_file"].callable_fn(path="../outside.txt")
        assert out.startswith("error:")

    def test_inner_traversal_refused(self, repo: Path) -> None:
        out = _tools_by_name(repo)["list_dir"].callable_fn(path="src/../..")
        assert out.startswith("error:")

    def test_tools_never_raise_on_garbage(self, repo: Path) -> None:
        for tool in make_planner_tools(repo):
            if tool.name == "grep_repo":
                assert tool.callable_fn(pattern="(unclosed").startswith("error:")
            else:
                assert tool.callable_fn(path="   ").startswith("error:")

    def test_symlink_escape_refused(self, repo: Path, tmp_path_factory) -> None:
        outside = tmp_path_factory.mktemp("outside") / "secret.txt"
        outside.write_text("escaped\n", encoding="utf-8")
        (repo / "sneaky_link.txt").symlink_to(outside)
        out = _tools_by_name(repo)["read_file"].callable_fn(path="sneaky_link.txt")
        assert out.startswith("error:")
        assert "escapes" in out


class TestListDir:
    def test_root_listing_with_dir_suffix_and_noise_pruned(self, repo: Path) -> None:
        out = _tools_by_name(repo)["list_dir"].callable_fn(path=".")
        assert "src/" in out
        assert "docs/" in out
        assert "__pycache__" not in out

    def test_subdir_listing(self, repo: Path) -> None:
        out = _tools_by_name(repo)["list_dir"].callable_fn(path="src/pkg")
        assert "alpha.py" in out
        assert "beta.py" in out

    def test_missing_dir_is_an_error_string(self, repo: Path) -> None:
        out = _tools_by_name(repo)["list_dir"].callable_fn(path="nope")
        assert out.startswith("error:")

    def test_entry_cap_announced(self, repo: Path) -> None:
        crowded = repo / "crowded"
        crowded.mkdir()
        for i in range(230):
            (crowded / f"f{i:03}.txt").write_text("x", encoding="utf-8")
        out = _tools_by_name(repo)["list_dir"].callable_fn(path="crowded")
        assert "more entries clipped" in out


class TestReadFile:
    def test_happy_path(self, repo: Path) -> None:
        out = _tools_by_name(repo)["read_file"].callable_fn(path="src/pkg/alpha.py")
        assert "def alpha" in out

    def test_truncation_note_on_large_file(self, repo: Path) -> None:
        big = repo / "src" / "big.py"
        big.write_text("x" * 30_000, encoding="utf-8")
        out = _tools_by_name(repo)["read_file"].callable_fn(path="src/big.py")
        assert len(out) < 30_000
        assert "truncated" in out

    def test_missing_file_is_an_error_string(self, repo: Path) -> None:
        out = _tools_by_name(repo)["read_file"].callable_fn(path="src/ghost.py")
        assert out.startswith("error:")

    def test_unicode_name_and_content_read_fine(self, repo: Path) -> None:
        target = repo / "docs" / "café — Tsitsipas, S..md"
        target.write_text("naïve unicode content ✓\n", encoding="utf-8")
        out = _tools_by_name(repo)["read_file"].callable_fn(path="docs/café — Tsitsipas, S..md")
        assert "naïve unicode content ✓" in out


class TestGrepRepo:
    def test_matches_with_path_line_format(self, repo: Path) -> None:
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern=r"def alpha")
        assert "src/pkg/alpha.py:1:" in out

    def test_glob_filters_files(self, repo: Path) -> None:
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern="alpha", glob="*.md")
        assert "docs/note.md:1:" in out
        assert ".py:" not in out

    def test_no_match_message(self, repo: Path) -> None:
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern="zzz_never_there")
        assert "no match" in out

    def test_invalid_regex_is_an_error_string(self, repo: Path) -> None:
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern="(unclosed")
        assert out.startswith("error:")

    def test_match_cap_announced(self, repo: Path) -> None:
        noisy = repo / "src" / "noisy.py"
        noisy.write_text("\n".join("needle = 1" for _ in range(120)), encoding="utf-8")
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern="needle")
        assert "more matches clipped" in out

    def test_pathological_regex_bounded_by_search_window(self, repo: Path) -> None:
        bomb = repo / "src" / "bomb.py"
        bomb.write_text("a" * 100_000 + "\n", encoding="utf-8")
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern=r"(a+)+$")
        assert isinstance(out, str)

    def test_grep_never_reads_through_an_escaping_symlink(
        self, repo: Path, tmp_path_factory
    ) -> None:
        outside = tmp_path_factory.mktemp("grep_outside") / "leak.py"
        outside.write_text("leaked_needle = 'secret'\n", encoding="utf-8")
        (repo / "src" / "leak.py").symlink_to(outside)
        out = _tools_by_name(repo)["grep_repo"].callable_fn(pattern="secret")
        assert "secret" not in out.replace("'secret'", "")
        assert "no match" in out
        assert "leak.py" not in out


class TestToolDefMetadata:
    def test_three_tools_with_expected_names_and_schemas(self, repo: Path) -> None:
        tools = _tools_by_name(repo)
        assert set(tools) == {"list_dir", "read_file", "grep_repo"}
        assert tools["list_dir"].parameters_schema["required"] == ["path"]
        assert tools["read_file"].parameters_schema["required"] == ["path"]
        assert tools["grep_repo"].parameters_schema["required"] == ["pattern"]


class TestReadFilePaging:
    """SP-DEV read paging — a clipped read must teach the model how to continue.

    Two Developer sessions burned their entire turn budget re-reading a
    ~1,200-line module the 24k cap could never serve whole; every window
    below pins that the response names the exact next ``start_line``.
    """

    @pytest.fixture()
    def numbered(self, repo: Path) -> Path:
        target = repo / "src" / "numbered.py"
        target.write_text("".join(f"L{n}\n" for n in range(1, 201)), encoding="utf-8")
        return repo

    def test_window_serves_exactly_the_requested_lines(self, numbered: Path) -> None:
        out = _tools_by_name(numbered)["read_file"].callable_fn(
            path="src/numbered.py", start_line=50, max_lines=10
        )
        assert out.splitlines()[0] == "L50"
        assert "L59" in out
        assert "L60" not in out.replace("start_line=60", "")
        assert "start_line=60" in out

    def test_window_reaching_eof_reports_end_of_file(self, numbered: Path) -> None:
        out = _tools_by_name(numbered)["read_file"].callable_fn(
            path="src/numbered.py", start_line=195
        )
        assert "L200" in out
        assert "[end of file: lines 195-200 of 200]" in out

    def test_capped_full_read_names_the_next_start_line(self, repo: Path) -> None:
        big = repo / "src" / "big_lines.py"
        big.write_text(
            "".join(f"row{n:04d} {'y' * 20}\n" for n in range(1, 2001)), encoding="utf-8"
        )
        first = _tools_by_name(repo)["read_file"].callable_fn(path="src/big_lines.py")
        assert "truncated at line" in first
        match = re.search(r"start_line=(\d+)", first)
        assert match is not None
        second = _tools_by_name(repo)["read_file"].callable_fn(
            path="src/big_lines.py", start_line=int(match.group(1))
        )
        assert f"row{int(match.group(1)):04d}" in second

    def test_string_paging_args_are_coerced(self, numbered: Path) -> None:
        out = _tools_by_name(numbered)["read_file"].callable_fn(
            path="src/numbered.py", start_line="50", max_lines="3"
        )
        assert out.splitlines()[0] == "L50"
        assert "start_line=53" in out

    def test_bad_paging_args_are_error_strings(self, numbered: Path) -> None:
        tools = _tools_by_name(numbered)
        assert (
            tools["read_file"]
            .callable_fn(path="src/numbered.py", start_line="abc")
            .startswith("error:")
        )
        assert (
            tools["read_file"]
            .callable_fn(path="src/numbered.py", start_line=0)
            .startswith("error:")
        )
        assert (
            tools["read_file"]
            .callable_fn(path="src/numbered.py", start_line=999)
            .startswith("error:")
        )

    def test_schema_advertises_the_paging_parameters(self, repo: Path) -> None:
        schema = _tools_by_name(repo)["read_file"].parameters_schema
        assert set(schema["properties"]) == {"path", "start_line", "max_lines"}
        assert schema["required"] == ["path"]
