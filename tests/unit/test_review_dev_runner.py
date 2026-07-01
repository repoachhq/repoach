"""Tests for the spec-driven Developer session runner."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from ferova.review.dev_runner import (
    DEFAULT_BRANCH_TEMPLATE,
    DevSessionResult,
    read_existing_files,
    run_developer_session,
    spec_to_branch_slug,
)
from ferova.review.spec import SPECS_DIR

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_spec(tmp_path: Path, name: str, content: str) -> Path:
    d = tmp_path / SPECS_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(content, encoding="utf-8")
    return p


_SAMPLE_PLAN = """\
# SP-FOO — Sample feature

This spec adds a single helper.

## Files

- modify `src/ferova/foo.py`
- add `tests/unit/test_foo_new.py`
- doc in `docs/runbooks/foo.md`
"""


# ---------------------------------------------------------------------------
# spec_to_branch_slug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec_id, expected",
    [
        ("SP-SEC", "sec"),
        ("SP-DEV", "dev"),
        ("SP-PB1", "pb1"),
        ("SP-arch-v2", "arch-v2"),
    ],
)
def test_spec_to_branch_slug(spec_id: str, expected: str) -> None:
    assert spec_to_branch_slug(spec_id) == expected


def test_default_branch_template_renders() -> None:
    out = DEFAULT_BRANCH_TEMPLATE.format(slug="sec")
    assert out == "feat/sp-sec-impl"


# ---------------------------------------------------------------------------
# read_existing_files
# ---------------------------------------------------------------------------


def test_read_existing_files_returns_only_existing(tmp_path: Path) -> None:
    """Only paths that exist on disk are returned; missing ones silently dropped."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("def foo(): ...\n", encoding="utf-8")

    _seed_spec(tmp_path, "2026-04-29_SP-FOO_sample.md", _SAMPLE_PLAN)
    from ferova.review.spec import load_spec

    plan = load_spec("FOO", root=tmp_path)
    files = read_existing_files(plan, repo_root=tmp_path)
    # foo.py exists → returned; test_foo_new.py and runbook don't → skipped.
    assert "src/ferova/foo.py" in files
    assert files["src/ferova/foo.py"].startswith("def foo")
    assert "tests/unit/test_foo_new.py" not in files
    assert "docs/runbooks/foo.md" not in files


def test_read_existing_files_rejects_traversal(tmp_path: Path) -> None:
    """Plans referencing escape paths get filtered (defence in depth)."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("ok", encoding="utf-8")
    plan_md = "# SP-X\n\nadd `../../escape.py` and `src/ferova/foo.py`\n"
    _seed_spec(tmp_path, "2026-04-29_SP-X_t.md", plan_md)
    from ferova.review.spec import load_spec

    plan = load_spec("X", root=tmp_path)
    files = read_existing_files(plan, repo_root=tmp_path)
    # Only the in-tree file ends up in the dict.
    assert "src/ferova/foo.py" in files
    assert all(".." not in p for p in files)


# ---------------------------------------------------------------------------
# run_developer_session — gates + outcomes (no real git push)
# ---------------------------------------------------------------------------


def _writing_developer(writes: list[tuple[str, str]]) -> MagicMock:
    """Build a Developer mock whose ``develop_step`` writes ``writes`` to disk.

    Mirrors the agentic loop's ``write_file`` tool calls (SP-DEVAGENT-LOOP); an
    empty list models a loop that ended without authoring anything.
    """
    from ferova.review.devagent_loop import DevLoopResult

    def _step(*, brief, repo_root, allowed_paths, repo_tree="", spec_id=None):
        for rel, content in writes:
            target = Path(repo_root) / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        return DevLoopResult(text="stub", model_used="stub", turns=1, tokens_used=0)

    dev = MagicMock()
    dev.develop_step.side_effect = _step
    return dev


def test_run_developer_session_spec_not_found(tmp_path: Path) -> None:
    """Missing spec → exit-5 reason, no Developer invocation."""
    (tmp_path / SPECS_DIR).mkdir(parents=True, exist_ok=True)
    dev = _writing_developer([])
    res = run_developer_session(
        "NOPE",
        repo_root=tmp_path,
        developer=dev,
        push=False,
        db_path=tmp_path / "test.db",
    )
    assert res.no_op_reason and "not found" in res.no_op_reason.lower()
    dev.develop_step.assert_not_called()


def _init_git_repo_with_plan(tmp_path: Path, *, step_files: list[str]) -> Path:
    """Seed a real git repo with the SP-FOO spec + a committed-ready plan.

    SP-DEV-PLAN-EXEC: the session is plan-first, so every session test
    seeds a plan (otherwise the runner would spawn a LIVE Planner —
    the no-LLM-sentinel rule for unit tests).
    """
    import subprocess

    from ferova.review.plan import ActionPlan, PlanStep, plan_relpath, render_plan_markdown

    _seed_spec(tmp_path, "2026-04-29_SP-FOO_t.md", _SAMPLE_PLAN)
    (tmp_path / "src").mkdir(exist_ok=True)
    (tmp_path / "tests" / "unit").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".gitignore").write_text(
        "__pycache__/\n*.db\n*.db-journal\n.pytest_cache/\n", encoding="utf-8"
    )
    for args in (
        ["init", "-q"],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
        ["add", "-A"],
        ["commit", "-q", "-m", "chore: init"],
    ):
        subprocess.run(["git", *args], cwd=tmp_path, capture_output=True, check=False)
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="Sample feature",
        summary="One step.",
        steps=[
            PlanStep(
                index=1,
                title="Do the step",
                files=step_files,
                action="Apply the fixes.",
                commit_message="feat(foo): step one",
                done_when="gates green",
                unit_tests=["tests/unit/test_foo_new.py"],
            )
        ],
        integration_tests=["tests/integration/test_foo_flow.py"],
    )
    target = tmp_path / plan_relpath("SP-FOO")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_plan_markdown(plan), encoding="utf-8")
    return tmp_path


def test_run_developer_session_no_fixes_returned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Developer writes nothing twice → step fails with a 'no writes' reason."""
    repo = _init_git_repo_with_plan(
        tmp_path, step_files=["src/ferova/foo.py", "tests/unit/test_foo_new.py"]
    )
    monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
    monkeypatch.setattr("ferova.review.dev_runner.render_repo_tree", lambda **kw: "")
    dev = _writing_developer([])
    res = run_developer_session(
        "FOO",
        repo_root=repo,
        developer=dev,
        push=False,
        db_path=tmp_path / "outside.db",
    )
    assert res.no_op_reason and "without writing any file" in res.no_op_reason.lower()
    assert res.fixes_applied == 0
    assert res.failed_step_index == 1
    assert res.plan_committed is True


def test_run_developer_session_all_fixes_rejected_by_whitelist(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Developer writes only out-of-contract paths → contract rejection."""
    repo = _init_git_repo_with_plan(
        tmp_path, step_files=["src/ferova/foo.py", "tests/unit/test_foo_new.py"]
    )
    monkeypatch.setattr("ferova.review.dev_runner.ensure_branch", lambda *a, **kw: True)
    monkeypatch.setattr("ferova.review.dev_runner.render_repo_tree", lambda **kw: "")
    dev = _writing_developer([(".env", "EVIL=1\n"), ("src/ferova/rogue.py", "x = 1\n")])
    res = run_developer_session(
        "FOO",
        repo_root=repo,
        developer=dev,
        push=False,
        db_path=tmp_path / "outside.db",
    )
    assert res.fixes_applied == 0
    assert res.fixes_rejected >= 2
    assert res.no_op_reason and "outside its contract" in res.no_op_reason.lower()


def test_dev_session_result_default_fields() -> None:
    """Sanity that the dataclass exposes the keys the CLI relies on."""
    res = DevSessionResult(spec_id="SP-X")
    assert res.spec_id == "SP-X"
    assert res.branch == ""
    assert res.fixes_applied == 0
    assert res.pushed is False
    assert res.pr_url == ""


# ---------------------------------------------------------------------------
# SP-DEV-V2-A: Python syntax pre-check
# ---------------------------------------------------------------------------


def test_check_python_syntax_passes_on_valid_files(tmp_path: Path) -> None:
    """Two valid .py files → (True, '')."""
    from ferova.review.dev_runner import check_python_syntax

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok1.py").write_text("def foo(): return 1\n", encoding="utf-8")
    (tmp_path / "src" / "ok2.py").write_text("x = 42\n", encoding="utf-8")

    ok, tail = check_python_syntax(tmp_path, ["src/ok1.py", "src/ok2.py"])
    assert ok is True
    assert tail == ""


def test_check_python_syntax_fails_on_invalid_indentation(tmp_path: Path) -> None:
    """Broken indentation → (False, '<path>:<line>: SyntaxError: ...')."""
    from ferova.review.dev_runner import check_python_syntax

    (tmp_path / "src").mkdir()
    bad = "def foo():\n    return 1\n  return 2\n"
    (tmp_path / "src" / "broken.py").write_text(bad, encoding="utf-8")

    ok, tail = check_python_syntax(tmp_path, ["src/broken.py"])
    assert ok is False
    assert "src/broken.py" in tail
    assert "SyntaxError" in tail


def test_check_python_syntax_skips_non_python_paths(tmp_path: Path) -> None:
    """Non-.py paths are skipped silently — no crash on .md / missing files."""
    from ferova.review.dev_runner import check_python_syntax

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "README.md").write_text("# hi\n", encoding="utf-8")

    ok, tail = check_python_syntax(tmp_path, ["docs/README.md", "src/missing.py"])
    # Non-py path skipped; missing .py path also skipped (silently).
    assert ok is True
    assert tail == ""


def test_check_python_syntax_catches_top_level_function_call(tmp_path: Path) -> None:
    """The exact failure mode observed on SP-MCP-EXT-A try 1: orphan call.

    The Developer NIM produced ``test_regression_guard_send_whatsapp()``
    at module top level instead of ``def test_...:``.  That is valid
    Python (a name lookup + call) but evaluates at import time and
    triggers NameError at collection.  ``compile()`` doesn't catch
    that — but it DOES catch the variant where the body is missing.
    """
    from ferova.review.dev_runner import check_python_syntax

    (tmp_path / "src").mkdir()
    # def with no body → SyntaxError
    bad = "def my_test():\n"
    (tmp_path / "src" / "incomplete.py").write_text(bad, encoding="utf-8")

    ok, tail = check_python_syntax(tmp_path, ["src/incomplete.py"])
    assert ok is False
    assert "incomplete.py" in tail


def test_check_python_syntax_empty_paths_returns_ok(tmp_path: Path) -> None:
    """No paths to check → ``(True, '')`` — common case when only docs change."""
    from ferova.review.dev_runner import check_python_syntax

    ok, tail = check_python_syntax(tmp_path, [])
    assert ok is True
    assert tail == ""


def test_check_python_syntax_handles_unicode_filename(tmp_path: Path) -> None:
    """UTF-8 file paths and contents are read without crashing."""
    from ferova.review.dev_runner import check_python_syntax

    (tmp_path / "src").mkdir()
    # Non-ASCII filename + non-ASCII content (Google docstring style ok).
    p = tmp_path / "src" / "café.py"
    p.write_text('"""Module accentué."""\n\nA = "été"\n', encoding="utf-8")
    ok, tail = check_python_syntax(tmp_path, ["src/café.py"])
    assert ok is True
    assert tail == ""
