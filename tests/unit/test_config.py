"""Unit tests for ``core.config.Settings`` anchoring.

SP-CONFIG-ENV-ANCHOR made env-file discovery independent of a stray
working directory; SP-UNIVERSAL-INSTALL moved the anchor itself from
the package's ancestor ``pyproject.toml`` to the git toplevel of the
working directory (``project_root``), replaced the host ``.env`` /
``chains.env`` pair with ``$XDG_CONFIG_HOME/repoach/env`` plus
``<project>/.repoach.env``, moved the ledger default under
``$XDG_DATA_HOME/repoach/<slug>/``, and moved the proxy-URL refusal
from construction to :func:`require_llm_proxy_base_url`.

Every test points ``project_root`` at an isolated ``tmp_path`` tree
rather than touching the real checkout's env files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import repoach.core.config as config


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    monkeypatch.setattr(config, "project_root", lambda: root)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    for name in (
        "REPOACH_DB_PATH",
        "REPOACH_ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
        "REPOACH_INTEGRATION_BRANCH",
        "INTEGRATION_BRANCH",
        "REPOACH_RELEASE_BRANCH",
        "RELEASE_BRANCH",
        "REPOACH_CODER_PYTHONS",
        "REPOACH_REVIEW_BOT_LOGIN",
        "REVIEW_BOT_LOGIN",
    ):
        monkeypatch.delenv(name, raising=False)
    return root


def test_project_env_file_sets_allow_listed_keys(project: Path) -> None:
    (project / ".repoach.env").write_text(
        "REPOACH_INTEGRATION_BRANCH=trunk\nRELEASE_BRANCH=stable\n", encoding="utf-8"
    )

    settings = config.Settings()

    assert settings.integration_branch == "trunk"
    assert settings.release_branch == "stable"


def test_project_env_file_overrides_user_env_file(project: Path, tmp_path: Path) -> None:
    user_dir = tmp_path / "config" / "repoach"
    user_dir.mkdir(parents=True)
    (user_dir / "env").write_text("REPOACH_INTEGRATION_BRANCH=user\n", encoding="utf-8")
    (project / ".repoach.env").write_text("REPOACH_INTEGRATION_BRANCH=project\n", encoding="utf-8")

    assert config.Settings().integration_branch == "project"


def test_project_env_file_cannot_set_endpoints_or_secrets(
    project: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    user_dir = tmp_path / "config" / "repoach"
    user_dir.mkdir(parents=True)
    (user_dir / "env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://user:1\nREPOACH_ANTHROPIC_AUTH_TOKEN=user-token\n",
        encoding="utf-8",
    )
    (project / ".repoach.env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://attacker.invalid\n"
        "ANTHROPIC_AUTH_TOKEN=attacker\n"
        "REPOACH_DB_PATH=/tmp/elsewhere.db\n"
        "REPOACH_CODER_PYTHONS=/tmp/evil-python\n"
        "REPOACH_REVIEW_BOT_LOGIN=attacker\n",
        encoding="utf-8",
    )

    settings = config.Settings()

    assert settings.llm_proxy_base_url == "http://user:1"
    assert settings.llm_proxy_auth_token is not None
    assert settings.llm_proxy_auth_token.get_secret_value() == "user-token"
    assert settings.db_path != Path("/tmp/elsewhere.db")
    assert settings.coder_pythons == ""
    assert settings.review_bot_login == "github-actions[bot]"
    assert "ignoring" in capsys.readouterr().err


def test_process_env_wins_over_an_allow_listed_project_key(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (project / ".repoach.env").write_text("REPOACH_INTEGRATION_BRANCH=project\n", encoding="utf-8")
    monkeypatch.setenv("INTEGRATION_BRANCH", "process")

    assert config.Settings().integration_branch == "process"


def test_user_env_file_is_read_without_project_file(project: Path, tmp_path: Path) -> None:
    user_dir = tmp_path / "config" / "repoach"
    user_dir.mkdir(parents=True)
    (user_dir / "env").write_text("REPOACH_LLM_PROXY_BASE_URL=http://user:1\n", encoding="utf-8")

    assert config.Settings().llm_proxy_base_url == "http://user:1"


def test_host_dotenv_and_chains_env_are_never_read(project: Path) -> None:
    """A foreign project's ``.env``/``chains.env`` is not repoach's configuration."""
    (project / ".env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://foreign-leak:1\n", encoding="utf-8"
    )
    (project / "chains.env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://foreign-leak:2\n", encoding="utf-8"
    )

    assert config.Settings().llm_proxy_base_url is None


def test_settings_build_without_any_gateway(project: Path) -> None:
    settings = config.Settings()

    assert settings.llm_proxy_base_url is None
    with pytest.raises(ValueError, match="llm_proxy_base_url unresolved"):
        config.require_llm_proxy_base_url(settings)


def test_explicit_env_var_wins_with_no_files(
    project: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_LLM_PROXY_BASE_URL", "http://127.0.0.1:8084")

    settings = config.Settings()

    assert config.require_llm_proxy_base_url(settings) == "http://127.0.0.1:8084"


def test_db_path_defaults_under_xdg_data_dir(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_cwd = tmp_path / "foreign_cwd"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)

    db_path = config.Settings().db_path

    assert db_path.is_absolute()
    assert db_path.is_relative_to(tmp_path / "data" / "repoach")
    assert db_path.name == "repoach.db"
    assert not db_path.is_relative_to(project)


def test_relative_db_path_override_resolves_against_project_root(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    foreign_cwd = tmp_path / "foreign_cwd"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setenv("REPOACH_DB_PATH", ".repoach/repoach.db")

    assert config.Settings().db_path == (project / ".repoach" / "repoach.db").resolve()


def test_db_path_absolute_env_override_untouched(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    explicit_db_path = tmp_path / "explicit" / "repoach.db"
    monkeypatch.setenv("REPOACH_DB_PATH", str(explicit_db_path))

    assert config.Settings().db_path == explicit_db_path
