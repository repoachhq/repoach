"""SP-UNIVERSAL-INSTALL backwards compatibility for repoach's own checkout.

A repoach checkout's untracked legacy ``.env`` and a regular
``data/repoach.db`` keep working (one deprecation notice each) until
``repoach migrate-config`` splits ``.env`` into the user-level file
(secrets, endpoints) and an untracked ``.repoach.env`` (allow-listed
keys), and moves the ledger -- under a SQLite write lock, verified,
refusing while another process holds it -- to the XDG default with a
symlink left behind.  A foreign project's ``.env`` is never read, nor a
tracked one.  The gateway's seed DB resolves to the same ledger.
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import repoach.core.config as config
import repoach.core.paths as paths
from repoach.cli.main import app
from repoach.core.paths import legacy_db_path as real_legacy_db_path
from repoach.core.paths import legacy_env_file as real_legacy_env_file
from repoach.llm_proxy.config.settings import Settings as GatewaySettings

LEGACY_ENV = (
    "REPOACH_LLM_PROXY_BASE_URL=http://legacy-gateway:8084\n"
    "REPOACH_ANTHROPIC_AUTH_TOKEN=owner-token\n"
    "REPOACH_INTEGRATION_BRANCH=develop\n"
    "REPOACH_NVIDIA_NIM_API_KEY=gateway-only\n"
)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _ledger(path: Path, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("CREATE TABLE pr_reviews (id INTEGER PRIMARY KEY, body TEXT)")
    connection.executemany(
        "INSERT INTO pr_reviews (body) VALUES (?)", [(f"r{i}",) for i in range(rows)]
    )
    connection.commit()
    connection.close()


def _count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return connection.execute("SELECT COUNT(*) FROM pr_reviews").fetchone()[0]
    finally:
        connection.close()


def _checkout(path: Path, *, name: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    _git(path, "remote", "add", "origin", "git@github.com:repoachhq/repoach.git")
    (path / "pyproject.toml").write_text(f'[project]\nname = "{name}"\n', encoding="utf-8")
    (path / ".env").write_text(LEGACY_ENV, encoding="utf-8")
    _ledger(path / "data" / "repoach.db", rows=25)
    return path


@pytest.fixture
def legacy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(paths, "legacy_env_file", real_legacy_env_file)
    monkeypatch.setattr(paths, "legacy_db_path", real_legacy_db_path)
    monkeypatch.setattr(paths, "_NOTIFIED", set())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    for name in (
        "REPOACH_DB_PATH",
        "REPOACH_LLM_PROXY_BASE_URL",
        "REPOACH_BREAKER_PROBE_SEED_DB",
        "REPOACH_ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_AUTH_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    return tmp_path


def _default_ledger(xdg: Path) -> Path:
    return xdg / "xdg_data" / "repoach" / "github.com" / "repoachhq" / "repoach" / "repoach.db"


def test_repoach_checkout_keeps_reading_legacy_env_and_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(legacy / "repoach", name="Repoach")
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


def test_tracked_legacy_env_in_a_repoach_fork_is_refused(
    legacy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(legacy / "fork", name="repoach")
    _git(root, "add", "-f", ".env")
    _git(root, "-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-q", "-m", "env")
    monkeypatch.chdir(root)

    assert config.Settings().llm_proxy_base_url is None
    assert f"refusing {root.resolve() / '.env'}" in capsys.readouterr().err


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
    assert _count(root / "data" / "repoach.db") == 25
    assert not (legacy / "xdg_data").exists()
    assert not (legacy / "xdg_config").exists()


def test_migrate_config_splits_env_and_moves_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    monkeypatch.chdir(root)
    destination = _default_ledger(legacy)
    user_env = legacy / "xdg_config" / "repoach" / "env"

    result = CliRunner().invoke(app, ["migrate-config"])

    assert result.exit_code == 0, result.output
    user_text = user_env.read_text(encoding="utf-8")
    assert "REPOACH_LLM_PROXY_BASE_URL" in user_text
    assert "REPOACH_ANTHROPIC_AUTH_TOKEN" in user_text
    assert "NVIDIA" not in user_text
    assert user_env.stat().st_mode & 0o777 == 0o600
    project_text = (root / ".repoach.env").read_text(encoding="utf-8")
    assert project_text == 'REPOACH_INTEGRATION_BRANCH="develop"\n'
    assert (root / ".env").read_text(encoding="utf-8") == LEGACY_ENV
    assert _count(destination) == 25
    assert (root / "data" / "repoach.db").is_symlink()
    assert (root / "data" / "repoach.db").resolve() == destination
    assert not (root / "data" / "repoach.db-wal").exists()

    capsys.readouterr()
    settings = config.Settings()
    assert settings.llm_proxy_base_url == "http://legacy-gateway:8084"
    assert settings.db_path == destination
    assert "deprecated" not in capsys.readouterr().err

    again = CliRunner().invoke(app, ["migrate-config"])
    assert again.exit_code == 0, again.output
    assert again.stdout.count("nothing to do") == 2


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
    assert _count(root / "data" / "repoach.db") == 25
    assert destination.read_bytes() == b"newer"


WRITER = """
import sqlite3, sys, time
connection = sqlite3.connect(sys.argv[1], timeout=30)
connection.execute("INSERT INTO pr_reviews (body) VALUES ('held')")
connection.commit()
print("ready", flush=True)
sys.stdin.readline()
connection.execute("INSERT INTO pr_reviews (body) VALUES ('after')")
connection.commit()
connection.close()
"""


def test_migrate_refuses_while_another_process_holds_the_ledger(
    legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    ledger = root / "data" / "repoach.db"
    monkeypatch.chdir(root)
    writer = subprocess.Popen(
        [sys.executable, "-c", WRITER, str(ledger)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert writer.stdout is not None and writer.stdout.readline().strip() == "ready"

        result = CliRunner().invoke(app, ["migrate-config"])

        assert result.exit_code == 2, result.output
        assert f"{writer.pid} (" in result.stderr
        assert "stop the gateway" in result.stderr
    finally:
        assert writer.stdin is not None
        writer.stdin.write("go\n")
        writer.stdin.flush()
        writer.wait(timeout=30)
    assert not ledger.is_symlink()
    assert _count(ledger) == 27
    assert not _default_ledger(legacy).exists()


def test_migrate_refuses_when_the_write_lock_is_held(
    legacy: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = _checkout(legacy / "repoach", name="repoach")
    ledger = root / "data" / "repoach.db"
    monkeypatch.chdir(root)
    monkeypatch.setattr("repoach.cli.migrate_cmds.LOCK_TIMEOUT_S", 0.2)
    holder = sqlite3.connect(ledger, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    holder.execute("INSERT INTO pr_reviews (body) VALUES ('in-flight')")
    try:
        result = CliRunner().invoke(app, ["migrate-config"])
    finally:
        holder.execute("COMMIT")
        holder.close()

    assert result.exit_code == 2, result.output
    assert "cannot lock" in result.stderr
    assert not ledger.is_symlink()
    assert _count(ledger) == 26
    assert not _default_ledger(legacy).exists()
