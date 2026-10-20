"""``review pr`` with a gateway URL but no shared secret fails before any forge call.

Measured 2026-10-20 (independent verification): ``REPOACH_LLM_PROXY_BASE_URL``
set, ``REPOACH_ANTHROPIC_AUTH_TOKEN`` unset → ``review pr`` read the forge
four times, then crashed with a ``ValueError`` traceback (rc 1) when the
agent loop was built. The forge here is a recording fake: any call fails
the test.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

import repoach.core.config as config
from repoach.cli.review_cmds import review_app


class _NoCallForge:
    """Forge boundary fake that refuses every call."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"forge touched: {name}")


@pytest.fixture
def gateway_without_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg_config"))
    monkeypatch.setenv("REPOACH_LLM_PROXY_BASE_URL", "http://gateway.test.invalid:1")
    monkeypatch.delenv("REPOACH_ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setenv("REPOACH_ENV", "dev")
    monkeypatch.setattr(config, "_settings", None)
    monkeypatch.setattr("repoach.review.orchestrator.select_forge", lambda **_kw: _NoCallForge())


def test_missing_token_is_a_named_error_before_any_forge_call(
    gateway_without_token: None,
) -> None:
    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert result.stderr.strip().splitlines() == [
        "repoach: REPOACH_ANTHROPIC_AUTH_TOKEN is not set -- the gateway at "
        "http://gateway.test.invalid:1 needs its shared secret; set it in the "
        "environment or ~/.config/repoach/env"
    ]
    assert result.stdout == ""
    assert "Traceback" not in result.output


def test_missing_url_is_a_named_error_before_any_forge_call(
    gateway_without_token: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setattr(config, "_settings", None)
    result = CliRunner().invoke(review_app, ["pr", "7", "--dry-run"])
    assert result.exit_code == 2, result.output
    assert "REPOACH_LLM_PROXY_BASE_URL" in result.stderr
