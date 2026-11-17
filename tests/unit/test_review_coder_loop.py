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

import pytest
import structlog
from structlog.testing import capture_logs

from repoach.forge.github import GhResult, GitHubForge
from repoach.review import coder_loop
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
from tests.forge_fakes import scripted_github

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


HEAD = "h" * 40
REQUIRED = ["Test suite (Python 3.13)"]


def _row(name: str, state: str, bucket: str, link: str = "") -> dict[str, str]:
    return {"name": name, "state": state, "bucket": bucket, "link": link, "workflow": "CI"}


def _run(name: str, conclusion: str | None, status: str = "completed") -> dict[str, object]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "startedAt": "2026-11-17T12:00:00Z",
        "completedAt": "2026-11-17T12:10:00Z" if conclusion else None,
        "headSha": HEAD,
    }


def _ci_gh(
    pr_checks: list[dict[str, str]],
    check_runs: list[dict[str, object]],
    *,
    pr_checks_rc: int = 0,
    seen: list[list[str]] | None = None,
) -> GitHubForge:
    """A GitHub forge answering ``gh pr checks`` and the head's check-runs.

    ``pr checks`` carries the rows a red is read from; the commit's
    check-runs (the ``--jq`` shape, one JSON object per line) carry the
    required checks the merge gate judges green on.
    """

    def _run_side(args: list[str]) -> GhResult:
        if seen is not None:
            seen.append(args)
        if args[:2] == ["pr", "checks"]:
            return GhResult(
                returncode=pr_checks_rc, stdout=json.dumps(pr_checks), stderr="", argv=args
            )
        if args[:1] == ["api"] and "/check-runs" in args[1]:
            lines = "\n".join(json.dumps(run) for run in check_runs)
            return GhResult(returncode=0, stdout=lines, stderr="", argv=args)
        return GhResult(returncode=0, stdout="", stderr="", argv=args)

    return scripted_github(run=_run_side)


def test_fetch_ci_status_green_when_the_required_checks_succeed() -> None:
    gh = _ci_gh([_row(REQUIRED[0], "SUCCESS", "pass")], [_run(REQUIRED[0], "success")])
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_GREEN, [])


def test_fetch_ci_status_unknown_when_no_check_ran() -> None:
    gh = _ci_gh([], [])
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_UNKNOWN, [])


def test_fetch_ci_status_pending_when_a_required_check_runs() -> None:
    gh = _ci_gh(
        [_row(REQUIRED[0], "IN_PROGRESS", "pending")],
        [_run(REQUIRED[0], None, status="in_progress")],
    )
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_PENDING, [])


def test_fetch_ci_status_red_when_a_check_failed() -> None:
    link = "https://github.com/o/r/actions/runs/42/job/100"
    gh = _ci_gh([_row(REQUIRED[0], "FAILURE", "fail", link)], [], pr_checks_rc=8)
    state, failed = fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD)
    assert state == CI_RED
    assert failed == [{"name": REQUIRED[0], "link": link, "workflow": "CI"}]


def test_fetch_ci_status_unknown_when_gh_errored() -> None:
    def _run_side(args: list[str]) -> GhResult:
        return GhResult(returncode=2, stdout="", stderr="boom", argv=args)

    gh = scripted_github(run=_run_side)
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_UNKNOWN, [])


@pytest.fixture(autouse=True)
def _fresh_coder_loop_logger(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rebind the module logger so ``capture_logs`` sees its events.

    ``configure_logging`` (exercised by earlier suites in serial order)
    sets ``cache_logger_on_first_use=True``; a proxy cached before this
    test keeps its materialized processor chain and bypasses the
    ``capture_logs`` swap. A fresh lazy proxy binds inside the capture
    context instead.
    """
    monkeypatch.setattr(coder_loop, "_log", structlog.get_logger("coder_loop.test"))


def test_fetch_ci_status_drops_required_flag() -> None:
    seen: list[list[str]] = []
    fetch_ci_status(_ci_gh([], [], seen=seen), 1, required=REQUIRED, head_sha=HEAD)

    assert seen[0][:2] == ["pr", "checks"]
    assert "--required" not in seen[0]


def test_fetch_ci_status_red_on_unprotected_repo_shape() -> None:
    """``--required`` on this repo's real unprotected branch errors out
    (no required checks configured to filter on); the full ``gh pr
    checks`` row set carries the actual RED signal.  A fake command
    runner distinguishes the two invocations exactly as the real ``gh``
    binary would on this repo, rather than monkeypatching the classifier
    directly.
    """

    def _run_side(args: list[str]) -> GhResult:
        if "--required" in args:
            return GhResult(
                returncode=1,
                stdout="",
                stderr="no required checks found for this pull request",
                argv=args,
            )
        return GhResult(
            returncode=8,
            stdout=json.dumps(
                [
                    _row(
                        "Test suite (Python 3.12)",
                        "SUCCESS",
                        "pass",
                        "https://github.com/o/r/actions/runs/42/job/101",
                    ),
                    _row(
                        "Test suite (Python 3.13)",
                        "FAILURE",
                        "fail",
                        "https://github.com/o/r/actions/runs/42/job/100",
                    ),
                ]
            ),
            stderr="",
            argv=args,
        )

    state, failed = fetch_ci_status(scripted_github(run=_run_side), 1)

    assert state == CI_RED
    assert failed == [
        {
            "name": "Test suite (Python 3.13)",
            "link": "https://github.com/o/r/actions/runs/42/job/100",
            "workflow": "CI",
        }
    ]


def test_fetch_ci_status_green_when_only_an_unrequired_check_skipped() -> None:
    gh = _ci_gh(
        [_row(REQUIRED[0], "SUCCESS", "pass"), _row("Docs build", "SKIPPED", "skipping")],
        [_run(REQUIRED[0], "success"), _run("Docs build", "skipped")],
    )
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_GREEN, [])


def test_fetch_ci_status_unknown_when_a_required_check_skipped() -> None:
    gh = _ci_gh(
        [_row(REQUIRED[0], "SKIPPED", "skipping")],
        [_run(REQUIRED[0], "skipped")],
    )
    with capture_logs() as logs:
        state = fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD)

    assert state == (CI_UNKNOWN, [])
    events = [e for e in logs if e.get("event") == "coder.ci_required_not_green"]
    assert len(events) == 1
    assert events[0]["outcome"] == "SKIP_CI_FAILED"
    assert f"{REQUIRED[0]}=SKIPPED" in events[0]["reason"]


def test_fetch_ci_status_green_with_cancelled_check() -> None:
    gh = _ci_gh(
        [_row(REQUIRED[0], "SUCCESS", "pass"), _row("Deploy preview", "CANCELLED", "cancel")],
        [_run(REQUIRED[0], "success"), _run("Deploy preview", "cancelled")],
    )
    assert fetch_ci_status(gh, 1, required=REQUIRED, head_sha=HEAD) == (CI_GREEN, [])


def test_fetch_failed_check_logs_pulls_run_view_log() -> None:
    canned = GhResult(returncode=0, stdout="boom\nTraceback...\nAssertionError", stderr="", argv=[])
    gh = scripted_github(run=lambda _args: canned)
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
