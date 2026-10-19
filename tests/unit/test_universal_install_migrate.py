"""SP-UNIVERSAL-INSTALL backwards compatibility for repoach's own checkout.

A repoach checkout's legacy ``.env`` and a regular ``data/repoach.db``
keep working (with one deprecation notice each) until
``repoach migrate-config`` links ``.repoach.env`` to ``.env`` and moves
the ledger to the XDG default, leaving a symlink behind.  A foreign
project's ``.env`` is never read.  The gateway's seed DB resolves to the
same ledger as the CLI.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.core.config as config
import repoach.core.paths as paths
from repoach.cli.main import app
from repoach.core.paths import legacy_db_path as real_legacy_db_path
from repoach.core.paths import legacy_env_file as real_legacy_env_file
from repoach.llm_proxy.config.settings import Settings as GatewaySettings


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _checkout(path: Path, *, name: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", "git@github.com:repoachhq/repoach.git")
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    (path / ".env").write_text(
        "REPOACH_LLM_PROXY_BASE_URL=http://legacy-gateway:8084\n", encoding="utf-8"
    )
    ledger = path / "data" / "repoach.db"
    ledger.parent.mkdir()
    ledger.write_bytes(b"ledger-bytes")
    (path / "data" / "repoach.db-wal").write_bytes(b"wal-bytes")
    return path


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "legacy_env_file", real_legacy_env_file)
    monkeypatch.setattr(paths, "legacy_db_path", real_legacy_db_path)
    monkeypatch.setattr(paths, "_NOTIFIED", set())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    for name in ("REPOACH_DB_PATH", "REPOACH_LLM_PROXY_BASE_URL", "REPOACH_BREAKER_PROBE_SEED_DB"):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _default_ledger(xdg: Path) -> Path:
    return xdg / "xdg_data" / "repoach" / "github.com" / "repoachhq" / "repoach" / "repoach.db"


def test_repoach_checkout_keeps_reading_legacy_env_and_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)

    settings = config.Settings()

    assert settings.llm_proxy_base_url == "http://legacy-gateway:8084"
    assert settings.db_path == root.resolve() / "data" / "repoach.db"
    notices = [line for line in capsys.readouterr().err.splitlines() if "deprecated" in line]
    assert len(notices) == 2
    assert any(str(root.resolve() / ".repoach.env") in line for line in notices)
    assert any(str(_default_ledger(legacy)) in line for line in notices)


def test_foreign_checkout_env_is_never_read(legacy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _checkout(legacy / "foreign", name="foreign")
    monkeypatch.chdir(root)

    assert config.Settings().llm_proxy_base_url is None


def test_gateway_seed_db_is_the_cli_ledger(legacy: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)

    gateway = GatewaySettings(_env_file=None)

    assert Path(gateway.breaker_probe_seed_db) == config.Settings().db_path


def test_migrate_config_dry_run_changes_nothing(
    legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)

    result = CliRunner().invoke(app, ["migrate-config", "--dry-run"])

    assert result.exit_code == 0, result.output
    assert not (root / ".repoach.env").exists()
    assert (root / "data" / "repoach.db").read_bytes() == b"ledger-bytes"
    assert not (legacy / "xdg_data").exists()


def test_migrate_config_links_env_and_moves_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)
    destination = _default_ledger(legacy)

    result = CliRunner().invoke(app, ["migrate-config"])

    assert result.exit_code == 0, result.output
    assert (root / ".repoach.env").is_symlink()
    assert (root / ".repoach.env").readlink() == Path(".env")
    assert destination.read_bytes() == b"ledger-bytes"
    assert destination.with_name("repoach.db-wal").read_bytes() == b"wal-bytes"
    assert (root / "data" / "repoach.db").is_symlink()
    assert (root / "data" / "repoach.db").resolve() == destination

    capsys.readouterr()
    settings = config.Settings()
    assert settings.llm_proxy_base_url == "http://legacy-gateway:8084"
    assert settings.db_path == destination
    assert "deprecated" not in capsys.readouterr().err

    again = CliRunner().invoke(app, ["migrate-config"])
    assert again.exit_code == 0, again.output
    assert "nothing to do" in again.stdout


def test_migrate_config_refuses_to_overwrite_a_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)
    destination = _default_ledger(legacy)
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"newer")

    result = CliRunner().invoke(app, ["migrate-config"])

    assert result.exit_code == 2
    assert "already exists" in result.stderr
    assert (root / "data" / "repoach.db").read_bytes() == b"ledger-bytes"
    assert destination.read_bytes() == b"newer"
