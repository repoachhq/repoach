"""Unit tests for SP-CONFIG-ENV-ANCHOR.

Pins the anchored env-file resolution on ``core.config.Settings``:
env-file discovery is rooted at the repository root (via a
monkeypatchable ``_repo_root`` seam), not the process's current working
directory, and the proxy base URL fails loud at construction instead
of silently defaulting to the retired, wrong ``http://localhost:8082``
literal.

Both tests point ``_repo_root`` at an isolated ``tmp_path`` fixture
tree rather than touching the real, deployed ``.env`` — CI checks out
no ``.env`` at all (it is gitignored) and the value there is a secret
this suite must never depend on.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import repoach.core.config as config


def test_env_files_anchored_from_foreign_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Env-file resolution follows the anchored root, not the CWD.

    The process is started from a foreign CWD holding neither
    ``chains.env`` nor ``.env`` — pre-fix, ``Settings`` resolved its
    (relative) ``env_file`` tuple against that CWD, found nothing, and
    silently fell back to the hardcoded ``http://localhost:8082``
    default. Anchored to a fixture "repo root" instead (independent of
    CWD), the same construction must load that root's ``.env`` and
    resolve the value written there.
    """
    anchored_root = tmp_path / "anchored_repo"
    anchored_root.mkdir()
    (anchored_root / ".env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://anchored-host:9999\n",
        encoding="utf-8",
    )
    foreign_cwd = tmp_path / "foreign_cwd"
    foreign_cwd.mkdir()

    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setattr(config, "_repo_root", lambda: anchored_root)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)

    settings = config.Settings()

    assert settings.llm_proxy_base_url == "http://anchored-host:9999"
    assert not settings.llm_proxy_base_url.rstrip("/").endswith(":8082"), (
        "must never resolve to the retired hardcoded :8082 fallback"
    )


def test_unresolved_base_url_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No anchored env file and no env var → fail loud, not :8082.

    The anchored root exists (so anchoring itself is not the failure
    mode under test) but carries neither ``chains.env`` nor ``.env``,
    and the process environment has no ``REPOACH_LLM_PROXY_BASE_URL``
    either — booting anyway would silently ship the
    ``llm_proxy_auth_token`` bearer secret at an unconfigured host.
    """
    anchored_root = tmp_path / "empty_anchored_repo"
    anchored_root.mkdir()

    monkeypatch.setattr(config, "_repo_root", lambda: anchored_root)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)

    with pytest.raises(ValueError, match="llm_proxy_base_url unresolved"):
        config.Settings()


def test_explicit_env_var_wins_with_no_anchored_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit env var resolves the URL even with nothing on disk.

    Mirrors A2 / the systemd deployment: the unit sets
    ``REPOACH_LLM_PROXY_BASE_URL`` directly and never relies on a file
    existing at all.
    """
    anchored_root = tmp_path / "empty_anchored_repo"
    anchored_root.mkdir()

    monkeypatch.setattr(config, "_repo_root", lambda: anchored_root)
    monkeypatch.setenv("REPOACH_LLM_PROXY_BASE_URL", "http://127.0.0.1:8084")

    settings = config.Settings()

    assert settings.llm_proxy_base_url == "http://127.0.0.1:8084"


def test_db_path_anchored_from_foreign_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The default ``db_path`` follows the anchored root, not the CWD.

    Pre-fix, ``db_path`` stayed the bare relative ``Path("./data/repoach.db")``
    with no anchoring validator at all, so it never resolved to an
    absolute path and this assertion fails against pre-change code
    (SP-DB-PATH-XDG AC1/AC3).
    """
    anchored_root = tmp_path / "anchored_repo"
    anchored_root.mkdir()
    foreign_cwd = tmp_path / "foreign_cwd"
    foreign_cwd.mkdir()

    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setattr(config, "_repo_root", lambda: anchored_root)
    monkeypatch.delenv("REPOACH_DB_PATH", raising=False)

    settings = config.Settings()

    assert settings.db_path == (anchored_root / "data" / "repoach.db").resolve()


def test_db_path_absolute_env_override_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit absolute ``REPOACH_DB_PATH`` passes through unchanged.

    Mirrors the CI shape (``.github/workflows/auto-review.yml`` sets an
    absolute ``runner.temp`` path) — the anchor must not rewrite an
    already-absolute value (SP-DB-PATH-XDG AC2/G3/NG2).
    """
    anchored_root = tmp_path / "anchored_repo"
    anchored_root.mkdir()
    foreign_cwd = tmp_path / "foreign_cwd"
    foreign_cwd.mkdir()
    explicit_db_path = tmp_path / "explicit" / "repoach.db"

    monkeypatch.chdir(foreign_cwd)
    monkeypatch.setattr(config, "_repo_root", lambda: anchored_root)
    monkeypatch.setenv("REPOACH_DB_PATH", str(explicit_db_path))

    settings = config.Settings()

    assert settings.db_path == explicit_db_path
