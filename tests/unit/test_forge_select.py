"""SP-UNIVERSAL-FORGE G4 / AC7 — ``select_forge`` picks the forge and its bot.

Each test builds a real git repository under ``tmp_path`` with a real
``origin`` URL; only ``shutil.which`` is faked when a binary must look
absent. Resolution order pinned here: ``--forge`` > ``REPOACH_FORGE`` >
``repoach.toml`` > origin host > ``github``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

from repoach.core import config
from repoach.forge import ForgeError, ForgeUnavailableError, MergeMethod, select_forge
from repoach.forge.github import GitHubForge
from repoach.forge.gitlab import GitLabForge
from repoach.forge.select import infer_forge_from_url, load_forge_config


@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Rebuild the settings singleton around each test so env changes apply."""
    monkeypatch.delenv("REPOACH_FORGE", raising=False)
    monkeypatch.delenv("REPOACH_REVIEW_BOT_LOGIN", raising=False)
    monkeypatch.delenv("REVIEW_BOT_LOGIN", raising=False)
    config._settings = None
    yield
    config._settings = None


def _repo(tmp_path: Path, origin: str | None, toml: str | None = None) -> Path:
    """A git repository with ``origin`` set (or none) and an optional ``repoach.toml``."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    if origin is not None:
        subprocess.run(["git", "-C", str(root), "remote", "add", "origin", origin], check=True)
    if toml is not None:
        (root / "repoach.toml").write_text(toml)
    return root


def _fake_binaries(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *present: str) -> None:
    """Make exactly ``present`` binaries resolvable through ``shutil.which``."""
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    found: dict[str, str] = {}
    for name in present:
        path = bindir / name
        path.write_text("")
        found[name] = str(path)

    def which(name: str) -> str | None:
        return found.get(name) or found.get(Path(name).name)

    monkeypatch.setattr("repoach.forge.github.shutil.which", which)
    monkeypatch.setattr("repoach.forge.gitlab.shutil.which", which)


def test_gitlab_scp_url_selects_gitlab(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(tmp_path, "git@gitlab.com:josephwfaye/yoob_2.git")
    assert isinstance(select_forge(cwd=root), GitLabForge)


def test_github_https_url_selects_github(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(tmp_path, "https://github.com/repoachhq/repoach.git")
    assert isinstance(select_forge(cwd=root), GitHubForge)


def test_missing_binary_raises_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh")
    root = _repo(tmp_path, "git@gitlab.com:josephwfaye/yoob_2.git")
    with pytest.raises(ForgeUnavailableError) as info:
        select_forge(cwd=root)
    assert info.value.binary == "glab"
    assert "glab" in str(info.value)


def test_flag_overrides_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(tmp_path, "https://github.com/repoachhq/repoach.git")
    assert isinstance(select_forge(override="gitlab", cwd=root), GitLabForge)


def test_env_overrides_toml_and_toml_overrides_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(tmp_path, "https://github.com/o/r.git", '[repoach]\nforge = "gitlab"\n')
    assert isinstance(select_forge(cwd=root), GitLabForge)
    monkeypatch.setenv("REPOACH_FORGE", "github")
    config._settings = None
    assert isinstance(select_forge(cwd=root), GitHubForge)


def test_unknown_host_and_no_origin_default_to_github(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    assert isinstance(
        select_forge(cwd=_repo(tmp_path, "https://git.example.org/o/r.git")), GitHubForge
    )
    other = tmp_path / "other"
    other.mkdir()
    assert isinstance(select_forge(cwd=_repo(other, None)), GitHubForge)


def test_unknown_forge_name_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    with pytest.raises(ForgeError, match="unknown forge"):
        select_forge(override="gitea", cwd=_repo(tmp_path, None))


def test_bot_username_defaults_per_forge(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub trusts ``github-actions[bot]`` by default; GitLab trusts nobody until configured."""
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(tmp_path, "https://github.com/o/r.git")
    assert select_forge(cwd=root).bot_username == "github-actions[bot]"
    assert select_forge(override="gitlab", cwd=root).bot_username == ""


def test_bot_username_from_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    root = _repo(
        tmp_path,
        "git@gitlab.com:o/r.git",
        '[repoach]\nmerge_method = "merge"\nremove_source_branch = false\n\n'
        '[review]\nbot_username = "project_1_bot"\n',
    )
    assert select_forge(cwd=root).bot_username == "project_1_bot"
    cfg = load_forge_config(root)
    assert cfg.merge_method is MergeMethod.MERGE and cfg.remove_source_branch is False


def test_explicit_env_bot_login_applies_to_gitlab(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_binaries(monkeypatch, tmp_path, "gh", "glab")
    monkeypatch.setenv("REPOACH_REVIEW_BOT_LOGIN", "token-user")
    config._settings = None
    root = _repo(tmp_path, "git@gitlab.com:o/r.git")
    assert select_forge(cwd=root).bot_username == "token-user"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@gitlab.com:josephwfaye/yoob_2.git", "gitlab"),
        ("https://gitlab.com/josephwfaye/yoob_2.git", "gitlab"),
        ("ssh://git@gitlab.example.com:2222/o/r.git", "gitlab"),
        ("https://github.com/repoachhq/repoach.git", "github"),
        ("git@github.com:repoachhq/repoach.git", "github"),
        ("https://mygitlabmirror.example.com/o/r.git", None),
        ("", None),
    ],
)
def test_infer_forge_from_url(url: str, expected: str | None) -> None:
    assert infer_forge_from_url(url) == expected
