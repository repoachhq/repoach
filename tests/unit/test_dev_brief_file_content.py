"""Tests for _embed_contract_files helper and build_step_brief wiring (SP-DEV-BRIEF-FILE-CONTENT)."""

from __future__ import annotations

from pathlib import Path

from ferova.review.dev_runner import (
    _EMBED_PER_FILE_CAP,
    _embed_contract_files,
    build_step_brief,
)
from ferova.review.plan import ActionPlan, PlanStep


def test_helper_embeds_existing_file_content(tmp_path: Path) -> None:
    """An existing contract file's content appears under a heading naming its path."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("def foo():\n    return 42\n", encoding="utf-8")

    result = _embed_contract_files(["src/ferova/foo.py"], repo_root=tmp_path)

    assert "## Existing contract files" in result
    assert "### `src/ferova/foo.py`" in result
    assert "def foo():" in result
    assert "return 42" in result


def test_helper_lists_missing_paths_under_to_create(tmp_path: Path) -> None:
    """Nonexistent contract paths appear under the 'Files to create' heading."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("ok\n", encoding="utf-8")

    result = _embed_contract_files(
        ["src/ferova/foo.py", "tests/unit/test_foo_new.py", "docs/runbooks/foo.md"],
        repo_root=tmp_path,
    )

    assert "## Existing contract files" in result
    assert "### `src/ferova/foo.py`" in result
    assert "## Files to create" in result
    assert "- `tests/unit/test_foo_new.py`" in result
    assert "- `docs/runbooks/foo.md`" in result


def test_helper_truncates_oversized_file_with_continuation_note(tmp_path: Path) -> None:
    """A file above the per-file cap is truncated with a read_file continuation note."""
    from ferova.review.dev_runner import _EMBED_PER_FILE_CAP

    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    big_content = "x = 1\n" * (_EMBED_PER_FILE_CAP // 6 + 500)
    (src / "big.py").write_text(big_content, encoding="utf-8")

    result = _embed_contract_files(["src/ferova/big.py"], repo_root=tmp_path)

    assert "### `src/ferova/big.py`" in result
    assert "[... truncated at" in result
    assert "read_file('src/ferova/big.py', start_line=" in result
    assert len(result) < len(big_content) + 200


def test_helper_handles_read_error_gracefully(tmp_path: Path) -> None:
    """A file that exists but cannot be read is listed with the error string."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    bad = src / "unreadable.py"
    bad.write_text("secret\n", encoding="utf-8")
    bad.chmod(0o000)

    result = _embed_contract_files(["src/ferova/unreadable.py"], repo_root=tmp_path)

    assert "## Existing contract files" in result
    assert "### `src/ferova/unreadable.py`" in result
    assert "[read error:" in result

    bad.chmod(0o644)


def test_helper_respects_total_budget(tmp_path: Path) -> None:
    """When the total budget is exhausted, remaining files get 'read on demand' notes."""
    from ferova.review.dev_runner import _EMBED_PER_FILE_CAP, _EMBED_TOTAL_BUDGET

    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)

    per_file = 10_000
    assert per_file < _EMBED_PER_FILE_CAP, "test files must fit under per-file cap"
    file_count = _EMBED_TOTAL_BUDGET // per_file + 2

    paths: list[str] = []
    for i in range(file_count):
        name = f"f{i}.py"
        paths.append(f"src/ferova/{name}")
        (src / name).write_text(f"x{i} = 1\n" * (per_file // 7), encoding="utf-8")

    result = _embed_contract_files(paths, repo_root=tmp_path)

    assert "read on demand" in result
    assert "budget exhausted" in result
    assert "### `src/ferova/f0.py`" in result


def _one_step_plan(files: list[str]) -> tuple[ActionPlan, PlanStep]:
    """Return a minimal one-step plan whose step's contract is *files*.

    The promised unit test file is always included in *files* so the
    plan-form validator accepts it.  When *files* touches ``src/`` an
    integration test promise is added to satisfy the src-interlock,
    and the integration test file is added to the step's contract so
    the validator's "promised but no step creates" check passes.
    """
    touches_src = any(f.startswith("src/") for f in files)
    all_files = list(files)
    test_file = "tests/unit/test_foo.py"
    if test_file not in all_files:
        all_files.append(test_file)
    integration_test_file = "tests/integration/test_foo_flow.py"
    if touches_src and integration_test_file not in all_files:
        all_files.append(integration_test_file)
    step = PlanStep(
        index=1,
        title="do the thing",
        action="implement it",
        files=all_files,
        commit_message="feat(foo): do the thing",
        done_when="tests pass",
        unit_tests=[f"{test_file}::test_something"],
    )
    plan = ActionPlan(
        spec_id="SP-FOO",
        title="Foo",
        summary="s",
        steps=[step],
        integration_tests=[integration_test_file] if touches_src else [],
    )
    return plan, step


def test_brief_embeds_existing_contract_files(tmp_path: Path, monkeypatch) -> None:
    """AC1: a brief for a step with one existing file embeds its content under a heading."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    (src / "foo.py").write_text("def foo():\n    return 42\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, step = _one_step_plan(["src/ferova/foo.py"])

    brief = build_step_brief(plan, step)

    assert "## Contract files" in brief
    assert "### `src/ferova/foo.py`" in brief
    assert "def foo():" in brief
    assert "return 42" in brief


def test_brief_lists_missing_contract_files_to_create(tmp_path: Path, monkeypatch) -> None:
    """AC2: nonexistent contract paths appear under the to-create heading."""
    monkeypatch.chdir(tmp_path)
    plan, step = _one_step_plan(["tests/unit/test_foo_new.py"])

    brief = build_step_brief(plan, step)

    assert "## Contract files" in brief
    assert "## Files to create" in brief
    assert "- `tests/unit/test_foo_new.py`" in brief


def test_oversized_file_truncated_with_continuation_note(tmp_path: Path, monkeypatch) -> None:
    """AC3: a file above the per-file cap is truncated with the exact continuation call."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    big_content = "x = 1\n" * (_EMBED_PER_FILE_CAP // 6 + 500)
    (src / "big.py").write_text(big_content, encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, step = _one_step_plan(["src/ferova/big.py"])

    brief = build_step_brief(plan, step)

    assert "### `src/ferova/big.py`" in brief
    assert "[... truncated at" in brief
    assert "read_file('src/ferova/big.py', start_line=" in brief


def test_retry_brief_reflects_disk_state(tmp_path: Path, monkeypatch) -> None:
    """AC4: after a simulated loop write, the retry brief carries the new content."""
    src = tmp_path / "src" / "ferova"
    src.mkdir(parents=True)
    target = src / "foo.py"
    target.write_text("def foo():\n    return 'before'\n", encoding="utf-8")

    monkeypatch.chdir(tmp_path)
    plan, step = _one_step_plan(["src/ferova/foo.py"])

    first_brief = build_step_brief(plan, step)
    assert "return 'before'" in first_brief

    target.write_text("def foo():\n    return 'after'\n", encoding="utf-8")

    retry_brief = build_step_brief(plan, step, gate_feedback="ruff gate: something failed")

    assert "return 'after'" in retry_brief
    assert "return 'before'" not in retry_brief
