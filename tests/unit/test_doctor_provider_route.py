"""``doctor`` says which provider route is live, and that a direct one is not routed.

SP-PROVIDER-DIRECT-ANTHROPIC (open). Measured 2026-10-20 on Yoob:
``REPOACH_PROVIDER=anthropic`` is parsed into ``[provider].kind`` but no
code routes on it; only ``REPOACH_LLM_PROXY_BASE_URL`` reaches a model.
Until the direct path ships, ``doctor`` names the gap instead of
implying the key is enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app
from tests.unit.test_universal_install import _repo


@pytest.fixture
def clone(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg_data"))
    monkeypatch.delenv("REPOACH_DB_PATH", raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    repo = _repo(tmp_path / "work" / "clone")
    monkeypatch.chdir(repo)
    return tmp_path


def test_anthropic_without_gateway_is_not_routed(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=anthropic (env; not routed)" in result.stdout
    assert "provider_route=none" in result.stdout
    assert "provider anthropic: not routed, set REPOACH_LLM_PROXY_BASE_URL" in result.stderr
    assert result.exit_code == 2


def test_anthropic_with_gateway_names_the_live_route(
    clone: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "anthropic")
    user_env = clone / "xdg_config" / "repoach" / "env"
    user_env.parent.mkdir(parents=True)
    user_env.write_text("REPOACH_LLM_PROXY_BASE_URL=http://gateway.test.invalid:1\n")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider_route=gateway (http://gateway.test.invalid:1)" in result.stdout
    assert "provider anthropic: not routed" in result.stderr


def test_gateway_kind_is_unchanged(clone: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOACH_PROVIDER", "gateway")
    result = CliRunner().invoke(app, ["doctor"])
    assert "provider=none" in result.stdout
    assert "not routed" not in result.stderr
