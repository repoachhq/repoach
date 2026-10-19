"""SP-UNIVERSAL-INSTALL: a tool install boots without a gateway, state stays off the checkout.

Pins the anchors of :mod:`repoach.core.paths` (git toplevel of the
working directory, XDG config/data directories, per-project ledger
slug), that ``repoach --version`` / ``repoach version`` never build
:class:`~repoach.core.config.Settings`, and that ``repoach doctor`` and
``repoach init`` refuse with rc 2 outside a git working tree and touch
nothing on a foreign repository.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.core.config as config
from repoach import __version__
from repoach.cli.main import app
from repoach.core.paths import (
    NotARepositoryError,
    default_db_path,
    project_root,
    project_slug,
    require_project_root,
    user_config_dir,
    user_data_dir,
)


def _git(cwd: Path, *args: str) -> str:
    completed = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout.strip()


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "develop")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "test")
    (path / "README").write_text("x\n", encoding="utf-8")
    _git(path, "add", "README")
    _git(path, "commit", "-q", "-m", "init")
    return path


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.delenv("REPOACH_DB_PATH", raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    return tmp_path


@pytest.fixture
def no_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any construction of ``Settings`` fail the test."""

    def _forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Settings must not be constructed for this command")

    monkeypatch.setattr(config, "get_settings", _forbidden)
    monkeypatch.setattr("repoach.core.logging.get_settings", _forbidden)
    monkeypatch.setattr(config.Settings, "__init__", _forbidden)


class TestProjectRoot:
    def test_cwd_inside_nested_worktree_resolves_to_the_worktree(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        outer = _repo(tmp_path / "outer")
        worktree = outer / ".claude" / "worktrees" / "feature"
        _git(outer, "worktree", "add", "-q", "-b", "feat/x", str(worktree))
        deep = worktree / "src" / "pkg"
        deep.mkdir(parents=True)
        monkeypatch.chdir(deep)

        assert project_root() == worktree.resolve()
        assert require_project_root() == worktree.resolve()
        assert project_root(outer / ".claude") == outer.resolve()

    def test_foreign_pyproject_ancestor_is_irrelevant(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        foreign = tmp_path / "foreign"
        (foreign / ".venv" / "lib").mkdir(parents=True)
        (foreign / "pyproject.toml").write_text('[project]\nname = "foreign"\n', encoding="utf-8")
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
        monkeypatch.chdir(cwd)

        assert project_root() == cwd.resolve()

    def test_outside_a_repository_falls_back_to_cwd_but_require_refuses(
        self, tmp_path: Path
    ) -> None:
        plain = tmp_path / "plain"
        plain.mkdir()

        assert project_root(plain) == plain.resolve()
        with pytest.raises(NotARepositoryError, match="not a git repository"):
            require_project_root(plain)

    def test_missing_git_raises_not_a_repository(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))

        with pytest.raises(NotARepositoryError, match="git executable not found"):
            project_root(tmp_path)


class TestXdgDirs:
    def test_xdg_overrides_are_honoured(self, xdg: Path) -> None:
        assert user_config_dir() == xdg / "xdg_config" / "repoach"
        assert user_data_dir() == xdg / "xdg_data" / "repoach"

    def test_relative_or_empty_xdg_values_fall_back_to_home(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
        monkeypatch.setenv("XDG_DATA_HOME", "")

        assert user_config_dir() == tmp_path / ".config" / "repoach"
        assert user_data_dir() == tmp_path / ".local" / "share" / "repoach"


class TestLedgerPath:
    def test_default_db_path_for_a_remote_less_repo(self, xdg: Path) -> None:
        repo = _repo(xdg / "work" / "my repo")

        db_path = default_db_path(repo)

        assert db_path.is_relative_to(xdg / "xdg_data" / "repoach" / "local")
        assert db_path.name == "repoach.db"
        assert db_path.parent.name.startswith("my-repo-")
        assert not db_path.is_relative_to(repo)
        assert db_path == default_db_path(repo)
        assert default_db_path(_repo(xdg / "work" / "other")) != db_path

    @pytest.mark.parametrize(
        ("remote", "slug"),
        [
            ("git@github.com:owner/repo.git", "github.com/owner/repo"),
            ("https://github.com/owner/repo", "github.com/owner/repo"),
            ("https://user:s3cret@gitlab.com/group/sub/repo.git", "gitlab.com/group/sub/repo"),
            ("ssh://git@gitlab.example.org:2222/team/repo.git", "gitlab.example.org/team/repo"),
        ],
    )
    def test_slug_from_origin_remote(self, xdg: Path, remote: str, slug: str) -> None:
        repo = _repo(xdg / "work" / "clone")
        _git(repo, "remote", "add", "origin", remote)

        assert project_slug(repo) == slug
        assert "s3cret" not in str(default_db_path(repo))
        assert default_db_path(repo) == xdg / "xdg_data" / "repoach" / slug / "repoach.db"

    def test_traversal_segments_never_escape_the_data_dir(self, xdg: Path) -> None:
        repo = _repo(xdg / "work" / "clone")
        _git(repo, "remote", "add", "origin", "https://example.org/../../etc/repo")

        db_path = default_db_path(repo).resolve()

        assert db_path.is_relative_to((xdg / "xdg_data" / "repoach").resolve())

    def test_worktrees_of_one_repository_share_a_slug(self, xdg: Path) -> None:
        repo = _repo(xdg / "work" / "clone")
        _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
        worktree = xdg / "work" / "wt"
        _git(repo, "worktree", "add", "-q", "-b", "feat/y", str(worktree))

        assert project_slug(worktree) == project_slug(repo)


class TestVersionNeverBuildsSettings:
    def test_dash_dash_version(self, no_settings: None) -> None:
        result = CliRunner().invoke(app, ["--version"])

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == f"repoach {__version__}"

    def test_version_subcommand_is_identical(self, no_settings: None) -> None:
        result = CliRunner().invoke(app, ["version"])

        assert result.exit_code == 0, result.output
        assert result.stdout.strip() == f"repoach {__version__}"


class TestDoctor:
    def test_outside_a_repository_exits_2_without_traceback(
        self, xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plain = xdg / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)

        result = CliRunner().invoke(app, ["doctor"])

        assert result.exit_code == 2
        assert "not a git repository" in result.stderr
        assert "Traceback" not in result.output
        assert isinstance(result.exception, SystemExit)

    def test_in_a_repository_reports_ledger_under_data_dir(
        self, xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(xdg / "work" / "clone")
        _git(repo, "remote", "add", "origin", "git@github.com:owner/repo.git")
        monkeypatch.chdir(repo)

        result = CliRunner().invoke(app, ["doctor"])

        lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        expected = xdg / "xdg_data" / "repoach" / "github.com" / "owner" / "repo" / "repoach.db"
        assert lines["project_root"] == str(repo.resolve())
        assert lines["db_path"] == str(expected)
        assert lines["provider"] == "none"
        assert result.exit_code == 2
        assert "no provider configured" in result.stderr
        assert not expected.parent.exists()

    def test_gateway_url_makes_the_provider_usable(
        self, xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(xdg / "work" / "clone")
        monkeypatch.chdir(repo)
        (repo / ".repoach.env").write_text(
            "REPOACH_LLM_PROXY_BASE_URL=http://gateway.test.invalid:1\n", encoding="utf-8"
        )

        result = CliRunner().invoke(app, ["doctor"])

        assert "provider=gateway (http://gateway.test.invalid:1)" in result.stdout


class TestInitOnAForeignRepository:
    def test_outside_a_repository_exits_2(self, xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        plain = xdg / "plain"
        plain.mkdir()
        monkeypatch.chdir(plain)

        result = CliRunner().invoke(app, ["init"])

        assert result.exit_code == 2
        assert "not a git repository" in result.stderr

    def test_plain_init_writes_nothing_and_keeps_hooks_path(
        self, xdg: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = _repo(xdg / "work" / "foreign")
        _git(repo, "config", "core.hooksPath", "scripts/hooks")
        monkeypatch.chdir(repo)

        result = CliRunner().invoke(app, ["init"])

        assert result.exit_code == 0, result.output
        assert _git(repo, "status", "--short") == ""
        assert _git(repo, "config", "--get", "core.hooksPath") == "scripts/hooks"
        assert not (repo / ".env").exists()
