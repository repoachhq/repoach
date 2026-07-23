"""SP-SECRET-ENV-UNIFY — integration: the planner's real env-building path.

``run_cc_exploration`` used to build the ``claude -p`` child env through a
local prefix-only filter (``planner_cc._scrubbed_env``, stripped only
``REPOACH_*``). It now routes through the shared marker-based
:func:`repoach.review.secret_env.scrubbed_env`, so unprefixed provider keys
and ``GITHUB_TOKEN``/``GH_TOKEN`` no longer reach a subprocess that reads
untrusted repository content. These tests exercise the real
:func:`run_cc_exploration` call — no Repoach function is monkeypatched, only
the real ``os.environ`` via ``monkeypatch.setenv``.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from repoach.review.planner_cc import run_cc_exploration


def _envelope() -> str:
    return json.dumps(
        {"type": "result", "is_error": False, "result": "{}", "num_turns": 1, "duration_ms": 1}
    )


def _capture_child_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return SimpleNamespace(stdout=_envelope(), stderr="", returncode=0)

    with patch("repoach.review.planner_cc.subprocess.run", side_effect=fake_run):
        run_cc_exploration(
            prompt="x", repo_root=Path("/repo"), model="sonnet", cli_path="claude-stub"
        )
    env = captured["env"]
    assert env is not None
    return env


def test_child_env_strips_github_and_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setenv("REPOACH_DB_PATH", "/tmp/db")

    env = _capture_child_env(monkeypatch)

    assert "GITHUB_TOKEN" not in env
    assert "OPENROUTER_API_KEY" not in env


def test_child_env_keeps_benign_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setenv("REPOACH_DB_PATH", "/tmp/db")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = _capture_child_env(monkeypatch)

    assert env.get("REPOACH_DB_PATH") == "/tmp/db"
    assert env.get("PATH") == "/usr/bin"
