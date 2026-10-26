"""SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB AC1/AC2: the instance token check.

A job proves the proxy answering ``/health`` is its own by comparing the
``instance`` the body echoes with the token it passed at start. These
tests pin the classifier, the CLI exit codes the shell helper relies on,
and the ``/health`` echo itself through a real FastAPI app.
"""

from __future__ import annotations

import socket

import pytest
from fastapi.testclient import TestClient

import repoach.llm_proxy.config.settings as proxy_settings_module
from repoach.llm_proxy import instance as instance_module
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.instance import (
    EXIT_FOREIGN,
    EXIT_OWN,
    EXIT_UNREACHABLE,
    EXIT_USAGE,
    INSTANCE_ENV,
    HealthMatch,
    check_health,
    classify_health,
    pick_free_port,
)


def test_classify_own_when_instance_matches() -> None:
    body = {"status": "healthy", "instance": "job-a"}
    assert classify_health(body, "job-a") is HealthMatch.OWN


def test_classify_foreign_when_instance_differs() -> None:
    body = {"status": "healthy", "instance": "job-b"}
    assert classify_health(body, "job-a") is HealthMatch.FOREIGN


def test_classify_foreign_when_healthy_proxy_echoes_no_instance() -> None:
    assert classify_health({"status": "healthy"}, "job-a") is HealthMatch.FOREIGN


@pytest.mark.parametrize("body", [None, "", [], {"status": "starting"}, {"instance": "job-a"}])
def test_classify_unreachable_when_nothing_healthy_answers(body: object) -> None:
    assert classify_health(body, "job-a") is HealthMatch.UNREACHABLE


def test_classify_refuses_an_empty_expected_token() -> None:
    with pytest.raises(ValueError):
        classify_health({"status": "healthy", "instance": ""}, "")


def test_check_health_names_the_foreign_proxy_host_port() -> None:
    match, message = check_health(
        "http://127.0.0.1:8083/health",
        "job-a",
        fetch=lambda _url: {"status": "healthy", "instance": "job-b"},
    )
    assert match is HealthMatch.FOREIGN
    assert message == "foreign proxy on 127.0.0.1:8083 (instance=job-b)"


def test_check_health_own_and_unreachable_messages() -> None:
    own, own_msg = check_health(
        "http://127.0.0.1:9/health",
        "job-a",
        fetch=lambda _url: {"status": "healthy", "instance": "job-a"},
    )
    assert own is HealthMatch.OWN
    assert own_msg == "own proxy on 127.0.0.1:9"
    gone, gone_msg = check_health("http://127.0.0.1:9/health", "job-a", fetch=lambda _url: None)
    assert gone is HealthMatch.UNREACHABLE
    assert gone_msg == "no healthy proxy on 127.0.0.1:9"


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"status": "healthy", "instance": "job-a"}, EXIT_OWN),
        ({"status": "healthy", "instance": "job-b"}, EXIT_FOREIGN),
        (None, EXIT_UNREACHABLE),
    ],
)
def test_cli_check_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: object,
    code: int,
) -> None:
    monkeypatch.setattr(instance_module, "fetch_health", lambda _url, timeout_s=1.0: body)
    rc = instance_module.main(["check", "--url", "http://127.0.0.1:1/health", "--expect", "job-a"])
    assert rc == code
    assert capsys.readouterr().out.strip()


def test_cli_check_refuses_empty_expect() -> None:
    assert instance_module.main(["check", "--url", "http://x/health", "--expect", ""]) == EXIT_USAGE


def test_cli_port_prints_a_bindable_port(capsys: pytest.CaptureFixture[str]) -> None:
    assert instance_module.main(["port"]) == EXIT_OWN
    port = int(capsys.readouterr().out.strip())
    assert 0 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))


def test_pick_free_port_avoids_a_bound_port() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as held:
        held.bind(("127.0.0.1", 0))
        held.listen()
        taken = held.getsockname()[1]
        assert pick_free_port() != taken


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


def test_health_echoes_instance_to_anonymous_and_authenticated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, "REALTOKEN", "run-1-review-abc")
    anon = client.get("/health").json()
    assert anon == {"status": "healthy", "instance": "run-1-review-abc"}
    auth = client.get("/health", headers={"x-api-key": "REALTOKEN"}).json()
    assert auth["instance"] == "run-1-review-abc"
    assert isinstance(auth["breaker"], list)
    assert classify_health(anon, "run-1-review-abc") is HealthMatch.OWN
    assert classify_health(anon, "run-2-review-def") is HealthMatch.FOREIGN


def test_health_without_instance_keeps_the_liveness_only_body(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch, "REALTOKEN", None)
    assert client.get("/health").json() == {"status": "healthy"}
