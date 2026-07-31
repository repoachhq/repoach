"""SP-SECRET-ENV-UNIFY — the shared marker-based env scrubber.

Pins the widened ``_SECRET_ENV_MARKERS`` set: every secret-bearing name the
2026-07-13 audit called out (unprefixed provider keys, GitHub tokens,
auth/bearer-style names, the ledger DSN) is stripped, while the non-secret
config every subprocess needs survives.
"""

from __future__ import annotations

import pytest

from repoach.review.secret_env import scrubbed_env


def test_markers_cover_auth_bearer_database_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-secret")
    monkeypatch.setenv("GH_TOKEN", "gh-secret")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-secret")
    monkeypatch.setenv("NVIDIA_NIM_API_KEY", "nim-secret")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "auth-secret")
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pass@host/db")
    monkeypatch.setenv("MY_BEARER_TOKEN", "bearer-secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("HOME", "/home/operator")
    monkeypatch.setenv("REPOACH_DB_PATH", "/var/lib/repoach/data.db")

    env = scrubbed_env()

    assert "GITHUB_TOKEN" not in env
    assert "GH_TOKEN" not in env
    assert "OPENROUTER_API_KEY" not in env
    assert "NVIDIA_NIM_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "DATABASE_URL" not in env
    assert "MY_BEARER_TOKEN" not in env
    assert env.get("PATH") == "/usr/bin"
    assert env.get("HOME") == "/home/operator"
    assert env.get("REPOACH_DB_PATH") == "/var/lib/repoach/data.db"
