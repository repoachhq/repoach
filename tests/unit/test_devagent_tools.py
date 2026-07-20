"""Tests for SP-DEVAGENT-TOOLS (slice 1): the Developer author + verify tools.

Focus: the sandbox (forbidden-path + traversal refusal), the write/edit
mechanics, and the error-string-never-raise contract; plus run_tests PASS/FAIL
against a throwaway repo.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from repoach.review.devagent_tools import make_developer_tools
from repoach.review.secret_env import scrubbed_env


def _tools(repo_root: Path) -> dict:
    return {tool.name: tool.callable_fn for tool in make_developer_tools(repo_root)}


def test_toolbox_shape() -> None:
    tools = make_developer_tools(Path.cwd())
    names = {tool.name for tool in tools}
    assert names == {"write_file", "edit_file", "run_tests", "run_ruff"}
    for tool in tools:
        assert isinstance(tool.parameters_schema, dict)


def test_write_file_creates_parent_dirs(tmp_path: Path) -> None:
    write = _tools(tmp_path)["write_file"]
    result = write("src/repoach/foo/bar.py", "x = 1\n")
    assert result.startswith("ok:")
    assert (tmp_path / "src/repoach/foo/bar.py").read_text() == "x = 1\n"


def test_write_file_rejects_forbidden_path(tmp_path: Path) -> None:
    write = _tools(tmp_path)["write_file"]
    result = write(".github/workflows/ci.yml", "evil")
    assert result.startswith("error:")
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_write_file_rejects_traversal(tmp_path: Path) -> None:
    write = _tools(tmp_path)["write_file"]
    result = write("../escape.py", "x = 1")
    assert result.startswith("error:")
    assert not (tmp_path.parent / "escape.py").exists()


def test_edit_file_applies_anchored_edit(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools["write_file"]("src/m.py", "a = 1\nb = 2\n")
    result = tools["edit_file"]("src/m.py", [{"search": "b = 2", "replace": "b = 3"}])
    assert result.startswith("ok:")
    assert (tmp_path / "src/m.py").read_text() == "a = 1\nb = 3\n"


def test_edit_file_missing_file_is_error(tmp_path: Path) -> None:
    edit = _tools(tmp_path)["edit_file"]
    result = edit("src/nope.py", [{"search": "x", "replace": "y"}])
    assert result.startswith("error:")
    assert "write_file" in result


def test_edit_file_absent_anchor_is_error(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools["write_file"]("src/m.py", "a = 1\n")
    result = tools["edit_file"]("src/m.py", [{"search": "does-not-exist", "replace": "z"}])
    assert result.startswith("error:")
    assert (tmp_path / "src/m.py").read_text() == "a = 1\n"


def test_edit_file_rejects_forbidden_path(tmp_path: Path) -> None:
    edit = _tools(tmp_path)["edit_file"]
    result = edit(".env", [{"search": "a", "replace": "b"}])
    assert result.startswith("error:")


def test_run_tests_pass_and_fail(tmp_path: Path) -> None:
    run_tests = _tools(tmp_path)["run_tests"]
    tests_dir = tmp_path / "suite"
    tests_dir.mkdir()
    (tests_dir / "test_ok.py").write_text("def test_ok():\n    assert True\n", encoding="utf-8")
    (tests_dir / "test_bad.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")

    passing = run_tests("suite/test_ok.py")
    assert passing.startswith("PASS")

    failing = run_tests("suite/test_bad.py")
    assert failing.startswith("FAIL")


def test_run_tests_rejects_traversal(tmp_path: Path) -> None:
    run_tests = _tools(tmp_path)["run_tests"]
    result = run_tests("../outside")
    assert result.startswith("error:")


def test_write_file_rejects_dotslash_forbidden_path(tmp_path: Path) -> None:
    write = _tools(tmp_path)["write_file"]
    result = write("./.github/workflows/ci.yml", "PWNED")
    assert result.startswith("error:")
    assert not (tmp_path / ".github/workflows/ci.yml").exists()


def test_write_file_rejects_symlink_to_forbidden_dir(tmp_path: Path) -> None:
    (tmp_path / ".github/workflows").mkdir(parents=True)
    (tmp_path / "sneaky").symlink_to(tmp_path / ".github/workflows")
    write = _tools(tmp_path)["write_file"]
    result = write("sneaky/evil.yml", "PWNED")
    assert result.startswith("error:")
    assert not (tmp_path / ".github/workflows/evil.yml").exists()


def test_non_string_path_is_error_not_raise(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    assert tools["write_file"](123, "x").startswith("error:")
    assert tools["edit_file"](None, [{"search": "a", "replace": "b"}]).startswith("error:")
    assert tools["run_tests"](123).startswith("error:")


def test_write_file_rejects_non_utf8_content(tmp_path: Path) -> None:
    write = _tools(tmp_path)["write_file"]
    result = write("src/x.py", "lone surrogate: \udc80")
    assert result.startswith("error:")
    assert not (tmp_path / "src/x.py").exists()


def test_edit_file_unencodable_replace_leaves_file_unchanged(tmp_path: Path) -> None:
    tools = _tools(tmp_path)
    tools["write_file"]("src/m.py", "a = 1\n")
    result = tools["edit_file"]("src/m.py", [{"search": "a = 1", "replace": "x = '\udc80'"}])
    assert result.startswith("error:")
    assert (tmp_path / "src/m.py").read_text() == "a = 1\n"


def test_run_tests_flaglike_target_not_consumed_as_flag(tmp_path: Path) -> None:
    run_tests = _tools(tmp_path)["run_tests"]
    result = run_tests("-x")
    assert not result.startswith("PASS")


def _contract_tools(repo_root: Path, allowed_paths: list[str]) -> dict:
    return {
        tool.name: tool.callable_fn
        for tool in make_developer_tools(repo_root, allowed_paths=allowed_paths)
    }


def test_allowed_paths_refuses_out_of_contract_write(tmp_path: Path) -> None:
    tools = _contract_tools(tmp_path, ["src/a.py"])
    assert tools["write_file"]("src/a.py", "x = 1\n").startswith("ok")
    refused = tools["write_file"]("src/b.py", "y = 1\n")
    assert refused.startswith("error")
    assert "contract" in refused
    assert not (tmp_path / "src" / "b.py").exists()


def test_allowed_paths_none_keeps_slice1_behaviour(tmp_path: Path) -> None:
    tools = {
        tool.name: tool.callable_fn for tool in make_developer_tools(tmp_path, allowed_paths=None)
    }
    assert tools["write_file"]("src/anything.py", "x = 1\n").startswith("ok")


def test_allowed_paths_normalises_dotslash_contract(tmp_path: Path) -> None:
    tools = _contract_tools(tmp_path, ["./src/a.py"])
    assert tools["write_file"]("src/a.py", "x = 1\n").startswith("ok")


def test_allowed_paths_jails_edit_too(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "b.py").write_text("x = 1\n", encoding="utf-8")
    tools = _contract_tools(tmp_path, ["src/a.py"])
    refused = tools["edit_file"]("src/b.py", [{"search": "x = 1", "replace": "x = 2"}])
    assert refused.startswith("error")
    assert "contract" in refused
    assert (tmp_path / "src" / "b.py").read_text() == "x = 1\n"


def test_scrubbed_env_strips_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_OPENROUTER_API_KEY", "live")
    monkeypatch.setenv("CLAUDE_CODE_ROUTINE_TOKEN", "live")
    monkeypatch.setenv("REPOACH_DB_PATH", "data/x.db")
    env = scrubbed_env()
    assert "REPOACH_OPENROUTER_API_KEY" not in env
    assert "CLAUDE_CODE_ROUTINE_TOKEN" not in env
    assert env.get("REPOACH_DB_PATH") == "data/x.db"
