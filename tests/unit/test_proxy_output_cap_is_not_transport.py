"""A reply past its output cap is the request's fault, never a transport error (SP-REVIEW-SURVIVES-TRANSPORT-FAILURES).

Measured 2026-11-04 on #288 (job proxy log ``server.log``, the CLI's own
transcript of session ``6dc7133d…``): the Sentinel's ``claude -p --model
opus`` call spent four replies of 4 096 thinking tokens and exited 1 with
``"api_error": "max_output_tokens"``; the proxy logged the head of the
CLI's JSON (200 characters of usage counts, cut before ``result``),
classified it ``provider_error``, tripped ``claude_code/opus``, and the
gateway answered an empty ``end_turn``; the reviewer asked again and the
refuter's judge calls that followed found every opus candidate tripped.

The boundary faked here is the ``claude`` subprocess, answering the JSON a
real ``claude`` 2.1.317 printed for the same fault
(``tests/fixtures/claude_code/max_output_tokens.json``, recorded with
``CLAUDE_CODE_MAX_OUTPUT_TOKENS=300``); the proxy app, its failover,
breaker and gateway dispatcher, the ``ProxyGatewayClient``, the agent loop
and the reviewer run unchanged.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
import yaml
from fastapi.testclient import TestClient
from loguru import logger
from pydantic import SecretStr

from repoach.agent_engine.adapters import ProxyGatewayClient
from repoach.agent_engine.agent_loop import AgentLoop
from repoach.agent_engine.transport_window import TransportWindow, configured_transport_window_s
from repoach.llm.capability import CapabilityTier
from repoach.llm_proxy.api.app import create_app
from repoach.llm_proxy.api.dependencies import get_settings
from repoach.llm_proxy.api.routes import get_proxy_service
from repoach.llm_proxy.api.runtime import report_configured_models_gone
from repoach.llm_proxy.api.services import ClaudeProxyService
from repoach.llm_proxy.config import settings as settings_module
from repoach.llm_proxy.config.settings import Settings
from repoach.llm_proxy.providers.base import ProviderConfig
from repoach.llm_proxy.providers.claude_code.client import ClaudeCodeProvider
from repoach.llm_proxy.providers.exceptions import OutputCapExceededError
from repoach.llm_proxy.routing import get_breaker, reset_breaker
from repoach.llm_proxy.routing.refs import ModelRef
from repoach.providers.errors import GatewayOutputCapError, GatewayTransportError
from repoach.review.review_retry import incomplete_cause
from repoach.review.reviewer import ReviewVerdict, Sentinel

REPO_ROOT = Path(__file__).resolve().parents[2]
RECORDED = REPO_ROOT / "tests" / "fixtures" / "claude_code" / "max_output_tokens.json"
OPUS = ModelRef.parse("claude_code/opus")


@pytest.fixture(autouse=True)
def _fresh_breaker() -> Any:
    reset_breaker()
    yield
    reset_breaker()


class _CliProcess:
    """The ``claude`` subprocess: exits ``returncode`` after printing ``stdout``."""

    pid = 424245

    def __init__(self, returncode: int, stdout: str, stderr: str = "") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout.encode(), self._stderr.encode()

    async def wait(self) -> int:
        return self.returncode


class _Spawns:
    """Counts every ``claude`` spawn and answers each with the recorded fault."""

    def __init__(self, returncode: int = 1, stdout: str | None = None) -> None:
        self.calls = 0
        self.envs: list[dict[str, str] | None] = []
        self.returncode = returncode
        self.stdout = RECORDED.read_text(encoding="utf-8") if stdout is None else stdout

    async def __call__(self, *args: Any, **kwargs: Any) -> _CliProcess:
        self.calls += 1
        self.envs.append(kwargs.get("env"))
        return _CliProcess(self.returncode, self.stdout)


def _client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("MODEL", "claude_code/opus")
    monkeypatch.setenv("MODEL_OPUS", "claude_code/opus")
    monkeypatch.setenv("REPOACH_BREAKER_ENABLED", "true")
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    settings = Settings(_env_file=None)
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")
    service = ClaudeProxyService(
        settings=settings,
        provider_getter=lambda _provider_id: provider,
        token_counter=lambda *args, **kwargs: 0,
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_proxy_service] = lambda: service
    return TestClient(app)


class _OpenApp:
    """The ``httpx.Client`` the gateway client opens per call: the app, already started."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    def __enter__(self) -> TestClient:
        return self._client

    def __exit__(self, *_exc: object) -> None:
        return None


def _gateway(monkeypatch: pytest.MonkeyPatch, client: TestClient) -> ProxyGatewayClient:
    monkeypatch.setattr(
        "repoach.agent_engine.adapters.httpx.Client", lambda **_kw: _OpenApp(client)
    )
    return ProxyGatewayClient(base_url="http://testserver", api_key="t", timeout_s=30.0)


def _ask(gateway: ProxyGatewayClient) -> Any:
    from repoach.llm_proxy.api.models.agent_v1 import Message, TextBlock

    return gateway.call(
        capability=CapabilityTier.OPUS,
        system=None,
        messages=[Message(role="user", content=[TextBlock(text="review this diff")])],
        tools=[],
        max_tokens=4096,
        temperature=0.1,
    )


def test_the_cli_error_keeps_its_result_text_and_names_the_cap() -> None:
    recorded = json.loads(RECORDED.read_text(encoding="utf-8"))
    assert recorded["api_error"] == "max_output_tokens"
    provider = ClaudeCodeProvider(ProviderConfig(api_key="unused"), cli_path="claude-stub")
    spawns = _Spawns()
    request = SimpleNamespace(
        model="claude-opus-4-7",
        system="",
        messages=[{"role": "user", "content": "review"}],
        tools=None,
        max_tokens=4096,
        thinking=None,
    )

    async def run() -> None:
        async for _event in provider.stream_response(request, request_id="req_1"):
            pass

    import asyncio

    with (
        patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            new=spawns,
        ),
        pytest.raises(OutputCapExceededError) as raised,
    ):
        asyncio.run(run())

    message = str(raised.value)
    assert message.startswith("claude CLI exited 1: API Error: Claude's response exceeded the 300")
    assert "CLAUDE_CODE_MAX_OUTPUT_TOKENS" in message
    assert "duration_api_ms" not in message
    assert spawns.envs[0] is not None
    assert spawns.envs[0]["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(4096 + 32_000)


def test_an_output_cap_fault_trips_nothing_and_reaches_the_caller_as_not_retryable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawns = _Spawns()
    client = _client(monkeypatch)
    with (
        client,
        patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            new=spawns,
        ),
    ):
        gateway = _gateway(monkeypatch, client)
        with pytest.raises(GatewayOutputCapError) as raised:
            _ask(gateway)

    assert raised.value.retryable is False
    assert not isinstance(raised.value, GatewayTransportError)
    assert "output_cap_exceeded" in str(raised.value)
    assert "exceeded the 300 output token maximum" in str(raised.value)
    assert spawns.calls == 1
    assert not get_breaker().is_down(OPUS, time.monotonic())


def test_the_reviewer_asks_once_and_the_review_is_not_retried_as_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("REPOACH_TRANSPORT_RETRY_WINDOW_S", "300")
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.configured_transport_window_s",
        configured_transport_window_s,
    )
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.get_settings",
        lambda: SimpleNamespace(
            llm_proxy_base_url="http://testserver", llm_proxy_auth_token=SecretStr("t")
        ),
    )
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)
    spawns = _Spawns()
    client = _client(monkeypatch)
    with (
        client,
        patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            new=spawns,
        ),
    ):
        loop = AgentLoop(capability=CapabilityTier.OPUS, max_tokens=4096)
        loop._client = _gateway(monkeypatch, client)
        outcome = Sentinel(loop=loop).review_diff(
            "diff --git a/x.py b/x.py\n+x = 1\n", pr_number=288
        )

    assert outcome.verdict is ReviewVerdict.ERROR
    assert outcome.summary.startswith(
        "[parse_failed:TRANSPORT] provider refused, not retried — GatewayOutputCapError"
    )
    assert spawns.calls == 1
    assert incomplete_cause(ReviewVerdict.ERROR, [outcome], []) == "other"


def test_a_tripped_chain_names_when_it_reopens_and_the_window_waits_only_that_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _client(monkeypatch)
    for ttl_s, expected_wait in ((120.0, 120.0), (604_800.0, None)):
        reset_breaker()
        get_breaker().trip(OPUS, now=time.monotonic(), ttl_s=ttl_s, reason="provider_error")
        with client:
            gateway = _gateway(monkeypatch, client)
            with pytest.raises(GatewayTransportError) as raised:
                _ask(gateway)
        assert raised.value.status == 502
        assert raised.value.retry_after_s == pytest.approx(ttl_s, abs=15.0)
        wait = TransportWindow(300.0, clock=lambda: 0.0).next_wait(raised.value, 1)
        if expected_wait is None:
            assert wait is None
        else:
            assert wait == pytest.approx(expected_wait, abs=15.0)


def test_a_configured_model_the_provider_retired_is_named_at_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "MODEL_OPUS", "nvidia_nim/z-ai/glm-5.2,open_router/z-ai/glm-5.2,claude_code/opus"
    )
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    settings = Settings(_env_file=None)
    now = time.monotonic()
    get_breaker().trip(
        ModelRef.parse("nvidia_nim/z-ai/glm-5.2"), now=now, ttl_s=604_800.0, reason="provider_410"
    )
    get_breaker().trip(
        ModelRef.parse("open_router/z-ai/glm-5.2"),
        now=now,
        ttl_s=21_600.0,
        reason="provider_402_propagated",
    )
    errors: list[str] = []
    sink = logger.add(lambda message: errors.append(str(message)), level="ERROR")
    try:
        lines = report_configured_models_gone(settings)
    finally:
        logger.remove(sink)

    assert lines == [
        "proxy_configured_model_gone: nvidia_nim/z-ai/glm-5.2 (provider_410) in MODEL_OPUS, "
        "tripped for 168 h more; remove it from chains.env"
    ]
    assert any("proxy_configured_model_gone: nvidia_nim/z-ai/glm-5.2" in e for e in errors)


def test_the_job_proxy_keeps_its_breaker_to_the_job() -> None:
    workflow = yaml.safe_load(
        (REPO_ROOT / ".github" / "workflows" / "auto-review.yml").read_text(encoding="utf-8")
    )
    starts = [
        step
        for job in workflow["jobs"].values()
        for step in job.get("steps", [])
        if step.get("name") == "Start LLM proxy sidecar"
    ]
    assert len(starts) == 2
    for step in starts:
        assert step["env"]["REPOACH_BREAKER_PROBE_SEED_DB"] == (
            "${{ runner.temp }}/job_proxy_breaker.db"
        )


def test_the_direct_claude_code_route_raises_the_same_error_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from repoach.core import config as core_config
    from tests.unit.test_provider_select import PROVIDER_ENV
    from tests.unit.test_universal_install import _repo

    monkeypatch.setenv("REPOACH_DB_PATH", str(tmp_path / "review.db"))
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("REPOACH_LLM_PROXY_BASE_URL", raising=False)
    monkeypatch.setenv("REPOACH_PROVIDER", "claude_code")
    monkeypatch.setenv("REPOACH_TRANSPORT_RETRY_WINDOW_S", "300")
    monkeypatch.setattr(
        "repoach.agent_engine.agent_loop.configured_transport_window_s",
        configured_transport_window_s,
    )
    monkeypatch.chdir(_repo(tmp_path / "clone"))
    monkeypatch.setattr(core_config, "_settings", None)
    spawns = _Spawns()
    with (
        patch(
            "repoach.llm_proxy.providers.claude_code.client.asyncio.create_subprocess_exec",
            new=spawns,
        ),
        pytest.raises(GatewayOutputCapError) as raised,
    ):
        AgentLoop(capability=CapabilityTier.OPUS).run_oneshot("review this diff")

    assert "exceeded the 300 output token maximum" in str(raised.value)
    assert spawns.calls == 1


def test_the_judge_asks_the_opus_tier_and_288s_chain_was_its_tripped_tail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Measured F15: the judge's ``[nvidia_nim/z-ai/glm-5.2]`` was the opus chain's head fallback.

    The refuter's judge is an OPUS one-shot (``make_refuter_judge``), sent
    as the alias ``claude-opus-4-7``, never as a literal ref; the opus
    chain served ``claude_code/opus`` until that tail was tripped too, then
    fell back to its head. The DEFAULT chain (the single ``MODEL``) has no
    tail at all; no review role routes to it.
    """
    from repoach.llm.capability import CAPABILITY_TO_ALIAS
    from repoach.llm_proxy.api.model_router import ModelRouter
    from repoach.llm_proxy.routing.tier import Tier, classify_tier

    monkeypatch.setenv("MODEL", "nvidia_nim/z-ai/glm-5.2")
    monkeypatch.setenv(
        "MODEL_OPUS", "nvidia_nim/z-ai/glm-5.2,open_router/z-ai/glm-5.2,claude_code/opus"
    )
    monkeypatch.setattr(settings_module, "_configured_env_files", lambda _cfg: ())
    router = ModelRouter(Settings(_env_file=None))
    alias = CAPABILITY_TO_ALIAS[CapabilityTier.OPUS]
    assert alias == "claude-opus-4-7"
    assert classify_tier(alias) is Tier.OPUS

    now = time.monotonic()
    for spec in ("nvidia_nim/z-ai/glm-5.2", "open_router/z-ai/glm-5.2"):
        get_breaker().trip(ModelRef.parse(spec), now=now, ttl_s=21_600.0, reason="provider_402")
    assert [c.provider_model_ref for c in router.resolve_chain(alias)] == ["claude_code/opus"]

    get_breaker().trip(OPUS, now=now, ttl_s=120.0, reason="provider_error")
    assert [c.provider_model_ref for c in router.resolve_chain(alias)] == [
        "nvidia_nim/z-ai/glm-5.2"
    ]
    assert [c.provider_model_ref for c in router.resolve_chain("gpt-4o")] == [
        "nvidia_nim/z-ai/glm-5.2"
    ]
