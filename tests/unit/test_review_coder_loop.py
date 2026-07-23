"""Tests for the shared Coder primitives in :mod:`repoach.review.coder_loop`.

The legacy ``run_coder_fix`` archive-verdict loop was deleted in redesign
slice 10b; this exercises the toolbox the findings-driven Coder
(:mod:`coder_findings`) and the Developer session build on (no network /
no real ``gh`` needed):

* :func:`is_path_allowed` — whitelist enforcement (forbidden paths,
  forbidden prefixes, parent traversal, absolute paths).
* :func:`apply_fixes` — happy path, whitelist rejection, escape
  prevention, bad-shape skipping.
* :func:`fetch_ci_status` / :func:`fetch_failed_check_logs` — CI state
  detection from ``gh pr checks`` and failed-log retrieval.
* :func:`run_pytest_matrix` / ``_pytest_pythons`` — the per-interpreter
  local pytest gate.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from repoach.review.coder_loop import (
    CI_GREEN,
    CI_PENDING,
    CI_RED,
    CI_UNKNOWN,
    apply_fixes,
    fetch_ci_status,
    fetch_failed_check_logs,
    is_path_allowed,
)
from repoach.review.gh_client import GhResult

# ---------------------------------------------------------------------------
# Whitelist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "src/repoach/foo.py",
        "tests/unit/test_x.py",
        "docs/open_work.md",
        "prompts/wa_chat/0.3.0.md",
    ],
)
def test_is_path_allowed_accepts_normal_paths(path: str) -> None:
    assert is_path_allowed(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "memory/L0_meta_rules.md",
        ".env",
        ".env.example",
        ".env.production",
        ".github/workflows/ci.yml",
        ".github/workflows/auto-review.yml",
        "prompts/review/coder_0.2.0.md",
        "prompts/review/architect_0.1.0.md",
        ".git/config",
    ],
)
def test_is_path_allowed_rejects_forbidden(path: str) -> None:
    assert is_path_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "/tmp/whatever",
        "../escape.py",
        "src/../../escape.py",
        "src/sub/../../../escape.py",
        "C:/Windows/system32",
        "",
    ],
)
def test_is_path_allowed_rejects_absolute_and_traversal(path: str) -> None:
    assert is_path_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        ".env.staging",
        ".envrc",
        "tests/fixtures/.env.test",
        "config/.env",
        "deploy/.envrc",
    ],
)
def test_is_path_allowed_rejects_env_family(path: str) -> None:
    assert is_path_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        ".githooks/pre-commit",
        ".githooks/pre-push",
        ".github/CODEOWNERS",
        ".github/dependabot.yml",
    ],
)
def test_is_path_allowed_rejects_github_and_githooks(path: str) -> None:
    assert is_path_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "chains.env",
        "a/b/chains.env",
    ],
)
def test_is_path_allowed_rejects_chains_env(path: str) -> None:
    assert is_path_allowed(path) is False


@pytest.mark.parametrize(
    "path",
    [
        "src/repoach/x.py",
        "tests/unit/x.py",
        "docs/x.md",
        "chains.env.bak",
        "mychains.env",
        "Chains.env",
    ],
)
def test_is_path_allowed_accepts_chains_env_lookalikes(path: str) -> None:
    assert is_path_allowed(path) is True


# ---------------------------------------------------------------------------
# apply_fixes
# ---------------------------------------------------------------------------


def test_apply_fixes_writes_files(tmp_path: Path) -> None:
    fixes = [
        {
            "path": "src/repoach/foo.py",
            "new_content": "print('hi')\n",
        },
        {
            "path": "tests/unit/test_foo.py",
            "new_content": "def test_x(): pass\n",
        },
    ]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 2
    assert rejected == []
    assert (tmp_path / "src/repoach/foo.py").read_text() == "print('hi')\n"
    assert (tmp_path / "tests/unit/test_foo.py").read_text() == "def test_x(): pass\n"


def test_apply_fixes_rejects_forbidden_paths(tmp_path: Path) -> None:
    fixes = [
        {"path": "memory/L0_meta_rules.md", "new_content": "evil"},
        {"path": ".github/workflows/ci.yml", "new_content": "evil"},
        {"path": "prompts/review/sentinel_0.1.0.md", "new_content": "evil"},
        {"path": ".env", "new_content": "GH_TOKEN=ghp_evil"},
        {"path": "src/legit.py", "new_content": "ok\n"},
    ]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 1
    assert len(rejected) == 4
    assert "memory/L0_meta_rules.md" in rejected
    assert ".github/workflows/ci.yml" in rejected
    assert ".env" in rejected
    # The legit one wrote.
    assert (tmp_path / "src/legit.py").exists()
    # The forbidden ones did NOT write.
    assert not (tmp_path / "memory/L0_meta_rules.md").exists()
    assert not (tmp_path / ".env").exists()


def test_apply_fixes_rejects_chains_env(tmp_path: Path) -> None:
    fixes = [
        {"path": "chains.env", "new_content": "REPOACH_CHAINS=evil\n"},
        {"path": "src/legit.py", "new_content": "ok\n"},
    ]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 1
    assert rejected == ["chains.env"]
    assert (tmp_path / "src/legit.py").exists()
    assert not (tmp_path / "chains.env").exists()


def test_apply_fixes_rejects_traversal(tmp_path: Path) -> None:
    fixes = [
        {"path": "../escape.py", "new_content": "evil"},
        {"path": "src/../../escape.py", "new_content": "evil"},
        {"path": "/abs/path.py", "new_content": "evil"},
    ]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 0
    assert len(rejected) == 3


def test_apply_fixes_rejects_dot_normalised_forbidden_paths(tmp_path: Path) -> None:
    fixes = [
        {"path": "./.github/workflows/x.yml", "new_content": "print('owned')\n"},
        {"path": ".//.githooks/pre-commit", "new_content": "print('owned')\n"},
        {"path": "./.git/hooks/pre-commit", "new_content": "print('owned')\n"},
        {"path": "./prompts/review/x.md", "new_content": "print('owned')\n"},
        {"path": "./src/legit.py", "new_content": "print('ok')\n"},
    ]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 1
    assert rejected == [
        "./.github/workflows/x.yml",
        ".//.githooks/pre-commit",
        "./.git/hooks/pre-commit",
        "./prompts/review/x.md",
    ]
    assert not (tmp_path / ".github").exists()
    assert not (tmp_path / ".githooks").exists()
    assert not (tmp_path / ".git").exists()
    assert not (tmp_path / "prompts").exists()
    assert (tmp_path / "src/legit.py").read_text() == "print('ok')\n"


def test_apply_fixes_rejects_symlink_to_forbidden_target(tmp_path: Path) -> None:
    workflows = tmp_path / ".github" / "workflows"
    workflows.mkdir(parents=True)
    forbidden_target = workflows / "ci.yml"
    forbidden_target.write_text("name: ci\n", encoding="utf-8")
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "note.md").symlink_to(forbidden_target)
    fixes = [{"path": "docs/note.md", "new_content": "on: push\njobs: {}\n"}]
    applied, rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 0
    assert rejected == ["docs/note.md"]
    assert forbidden_target.read_text(encoding="utf-8") == "name: ci\n"


def test_apply_fixes_skips_bad_dict_shapes(tmp_path: Path) -> None:
    fixes = [
        {"path": "src/ok.py", "new_content": "x\n"},
        {"path": "src/no_content.py"},
        {"new_content": "x\n"},
        {"path": 42, "new_content": "x\n"},
        {"path": "src/non_str.py", "new_content": 123},
    ]
    applied, _rejected = apply_fixes(fixes, repo_root=tmp_path)
    assert applied == 1
    assert (tmp_path / "src/ok.py").exists()


# ---------------------------------------------------------------------------
# CI status detection
# ---------------------------------------------------------------------------


def _ci_only_gh(state: str) -> MagicMock:
    """Build a GhCli mock that only answers ``pr checks`` calls."""
    gh = MagicMock()

    def _run_side(args: list[str]) -> GhResult:
        if args[:2] != ["pr", "checks"]:
            return GhResult(returncode=0, stdout="", stderr="", argv=args)
        if state == CI_GREEN:
            return GhResult(returncode=0, stdout="[]", stderr="", argv=args)
        if state == CI_PENDING:
            return GhResult(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "name": "x",
                            "state": "IN_PROGRESS",
                            "bucket": "pending",
                            "link": "",
                            "workflow": "CI",
                        }
                    ]
                ),
                stderr="",
                argv=args,
            )
        if state == CI_RED:
            return GhResult(
                returncode=8,
                stdout=json.dumps(
                    [
                        {
                            "name": "Test suite (Python 3.13)",
                            "state": "FAILURE",
                            "bucket": "fail",
                            "link": "https://github.com/o/r/actions/runs/42/job/100",
                            "workflow": "CI",
                        }
                    ]
                ),
                stderr="",
                argv=args,
            )
        if state == CI_UNKNOWN:
            return GhResult(returncode=2, stdout="", stderr="boom", argv=args)
        raise ValueError(f"unknown ci state: {state}")

    gh._run.side_effect = _run_side
    return gh


def test_fetch_ci_status_green_when_no_required_checks() -> None:
    state, failed = fetch_ci_status(_ci_only_gh(CI_GREEN), 1)
    assert state == CI_GREEN
    assert failed == []


def test_fetch_ci_status_pending_when_any_check_pending() -> None:
    state, failed = fetch_ci_status(_ci_only_gh(CI_PENDING), 1)
    assert state == CI_PENDING
    assert failed == []


def test_fetch_ci_status_red_when_a_check_failed() -> None:
    state, failed = fetch_ci_status(_ci_only_gh(CI_RED), 1)
    assert state == CI_RED
    assert len(failed) == 1
    assert failed[0]["name"] == "Test suite (Python 3.13)"


def test_fetch_ci_status_unknown_when_gh_errored() -> None:
    state, failed = fetch_ci_status(_ci_only_gh(CI_UNKNOWN), 1)
    assert state == CI_UNKNOWN
    assert failed == []


def test_fetch_failed_check_logs_pulls_run_view_log() -> None:
    gh = MagicMock()
    gh._run.return_value = GhResult(
        returncode=0, stdout="boom\nTraceback...\nAssertionError", stderr="", argv=[]
    )
    rows = [
        {
            "name": "Test suite (Python 3.13)",
            "link": "https://github.com/o/r/actions/runs/42/job/100",
            "workflow": "CI",
        }
    ]
    logs = fetch_failed_check_logs(gh, rows)
    assert len(logs) == 1
    assert "Test suite (Python 3.13)" in logs[0]
    assert "Traceback" in logs[0]


# ---------------------------------------------------------------------------
# pytest matrix — bot must validate on every CI Python version locally.
# ---------------------------------------------------------------------------


def test_pytest_pythons_returns_default_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env → single ``[None]`` slot (uses bare ``pytest`` on PATH)."""
    from repoach.review.coder_loop import _pytest_pythons

    monkeypatch.delenv("REPOACH_CODER_PYTHONS", raising=False)
    assert _pytest_pythons() == [None]


def test_pytest_pythons_filters_missing_interpreters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only interpreters resolvable by ``shutil.which`` are kept."""
    import shutil as _shutil

    from repoach.review.coder_loop import _pytest_pythons

    monkeypatch.setenv("REPOACH_CODER_PYTHONS", "python3.11,python-does-not-exist,python3.13")

    def _fake_which(name: str) -> str | None:
        if name == "python3.11":
            return "/opt/python3.11"
        if name == "python3.13":
            return "/opt/python3.13"
        return None

    monkeypatch.setattr(_shutil, "which", _fake_which)
    assert _pytest_pythons() == ["python3.11", "python3.13"]


def test_pytest_pythons_falls_back_when_none_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All listed interpreters missing → fall back to ``[None]``."""
    import shutil as _shutil

    from repoach.review.coder_loop import _pytest_pythons

    monkeypatch.setenv("REPOACH_CODER_PYTHONS", "python-no,python-also-no")
    monkeypatch.setattr(_shutil, "which", lambda _name: None)
    assert _pytest_pythons() == [None]


def test_run_pytest_matrix_short_circuits_on_first_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Matrix iterates; first red slot returns immediately."""
    from repoach.review import coder_loop as cl

    monkeypatch.setattr(cl, "_pytest_pythons", lambda: ["python3.11", "python3.13"])

    calls: list[str | None] = []

    def _fake_run_pytest(repo, *, python=None):
        calls.append(python)
        # First slot fails — second must NOT be called.
        if python == "python3.11":
            return False, "FAILED on 3.11"
        return True, "ok"

    monkeypatch.setattr(cl, "run_pytest", _fake_run_pytest)
    ok, tail = cl.run_pytest_matrix(tmp_path)
    assert ok is False
    assert "3.11" in tail
    assert calls == ["python3.11"]


def test_run_pytest_matrix_runs_all_slots_when_green(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """All slots green → runs every slot and reports the summary."""
    from repoach.review import coder_loop as cl

    monkeypatch.setattr(cl, "_pytest_pythons", lambda: ["python3.11", "python3.13"])

    calls: list[str | None] = []

    def _fake_run_pytest(repo, *, python=None):
        calls.append(python)
        return True, "ok"

    monkeypatch.setattr(cl, "run_pytest", _fake_run_pytest)
    ok, tail = cl.run_pytest_matrix(tmp_path)
    assert ok is True
    assert calls == ["python3.11", "python3.13"]
    assert "python3.11" in tail and "python3.13" in tail


def test_run_pytest_scrubs_secret_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """run_pytest runs agent-authored tests with a secret-scrubbed env (S4)."""
    import repoach.review.coder_loop as cl

    monkeypatch.setenv("REPOACH_OPENROUTER_API_KEY", "live-secret")
    monkeypatch.setenv("REPOACH_DB_PATH", "data/x.db")
    captured: dict[str, object] = {}

    class _Proc:
        returncode = 0
        stdout = ""
        stderr = ""

    def _capture(argv, **kwargs):
        captured["env"] = kwargs.get("env")
        return _Proc()

    monkeypatch.setattr(cl.subprocess, "run", _capture)
    cl.run_pytest(tmp_path)

    env = captured["env"]
    assert env is not None
    assert "REPOACH_OPENROUTER_API_KEY" not in env
    assert env.get("REPOACH_DB_PATH") == "data/x.db"
