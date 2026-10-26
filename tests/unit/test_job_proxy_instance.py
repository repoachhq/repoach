"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC2: ``/health`` echoes the job's instance token.

The token is the second ownership proof (the PID proof in
``scripts/job_proxy.sh`` is the first). Driven through a real FastAPI app
with hermetic settings, the same pattern as ``test_proxy_health_auth.py``.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.instance import INSTANCE_ENV, current_instance


def _client(monkeypatch: pytest.MonkeyPatch, token: str, instance: str | None) -> TestClient:
    monkeypatch.setattr(proxy_settings_module, "_configured_env_files", lambda _cfg: ())
    monkeypatch.setenv("REPOACH_ANTHROPIC_AUTH_TOKEN", token)
    if instance is None:
        monkeypatch.delenv(INSTANCE_ENV, raising=False)
    else:
        monkeypatch.setenv(INSTANCE_ENV, instance)
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: Settings(_env_file=None)
    return TestClient(app)


def test_current_instance_reads_the_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(INSTANCE_ENV, "run-1-review-abc")
    assert current_instance() == "run-1-review-abc"
    monkeypatch.delenv(INSTANCE_ENV)
    assert current_instance() == ""


def test_health_echoes_instance_to_anonymous_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, "REALTOKEN", "run-1-review-abc")
    anon = client.get("/health").json()
    assert anon == {"status": "healthy", "instance": "run-1-review-abc"}
    auth = client.get("/health", headers={"x-api-key": "REALTOKEN"}).json()
    assert auth["instance"] == "run-1-review-abc"
    assert isinstance(auth["breaker"], list)


def test_health_without_instance_keeps_the_liveness_only_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, "REALTOKEN", None)
    assert client.get("/health").json() == {"status": "healthy"}
