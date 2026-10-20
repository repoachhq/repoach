"""SP-UNIVERSAL-CI-ENTRY G10: ``repoach doctor --ci`` names variables, never values."""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from repoach.cli.main import app


class TestDoctorCi:
    def test_names_only(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.chdir(tmp_path)
        monkeypatch.setenv("GITLAB_CI", "true")
        monkeypatch.setenv("CI_MERGE_REQUEST_IID", "283")
        monkeypatch.setenv("REPOACH_GITLAB_TOKEN", "glpat-very-secret")
        monkeypatch.setenv("REPOACH_ANTHROPIC_API_KEY", "sk-ant-very-secret")
        monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
        monkeypatch.delenv("REPOACH_ANTHROPIC_AUTH_TOKEN", raising=False)
        result = CliRunner().invoke(app, ["doctor", "--ci"])
        assert result.exit_code == 0
        lines = result.stdout.splitlines()
        assert "ci=gitlab (GITLAB_CI)" in lines
        assert any(line.startswith("REPOACH_GITLAB_TOKEN=present") for line in lines)
        assert any(line.startswith("GH_TOKEN=absent") for line in lines)
        assert "pr_source=CI_MERGE_REQUEST_IID" in lines
        assert "provider_mode=direct" in lines
        assert "very-secret" not in result.stdout
        assert "283" not in result.stdout
