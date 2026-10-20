"""Start a loopback repoach gateway for one CI job (direct-provider mode).

A review reaches a model only through the repoach gateway
(``REPOACH_LLM_PROXY_BASE_URL``); the direct providers are parsed but not
routed yet (SP-PROVIDER-DIRECT-ANTHROPIC). A job holding only an
Anthropic key therefore runs the gateway inside itself: on
``127.0.0.1``, with that key as its only upstream and a shared secret
drawn per job, exactly as Yoob's ``ci/review-advisory.sh`` did in shell.

The gateway's environment holds the provider key and no forge
credential; the review's environment (built by
:mod:`repoach.ci_entry.advisory`) holds the forge credential and the
per-job secret, never the provider key. The gateway runs with a
temporary working directory so no project ``.env`` can override the
per-job secret.
"""

from __future__ import annotations

import secrets
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from ..core.logging import get_logger
from .context import env_value
from .variables import DIRECT_KEY_VARS, FORGE_TOKEN_VARS, GATEWAY_PORT_VAR

_log = get_logger(__name__)

DEFAULT_MODELS: dict[str, str] = {
    "MODEL_OPUS": "claude-opus-5",
    "MODEL_SONNET": "claude-sonnet-5",
    "MODEL_HAIKU": "claude-haiku-4-5",
}
"""Anthropic model per gateway tier; ``REPOACH_MODEL_<TIER>`` overrides each."""

LOOPBACK_HOST = "127.0.0.1"

HEALTH_TIMEOUT_S = 60.0
"""How long the gateway gets to answer ``/health`` before the run gives up."""

LOG_TAIL_LINES = 20


class GatewayStartError(Exception):
    """The loopback gateway did not answer ``/health``; carries its log tail."""


class GatewayHandle(BaseModel):
    """A running loopback gateway, as the review sees it.

    Attributes:
        base_url: ``http://127.0.0.1:<port>``.
        auth_token: The per-job shared secret.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str
    auth_token: str


def free_port() -> int:
    """Return a loopback TCP port free at the time of the call."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK_HOST, 0))
        return int(sock.getsockname()[1])


def gateway_env(env: Mapping[str, str], *, port: int, auth_token: str) -> dict[str, str]:
    """Build the gateway's environment: the provider key in, every forge credential out.

    Args:
        env: The job environment.
        port: The loopback port to bind.
        auth_token: The per-job shared secret.

    Returns:
        The environment for ``python -m repoach.llm_proxy``.
    """
    child = {k: v for k, v in env.items() if k not in FORGE_TOKEN_VARS}
    key_name = next(name for name in DIRECT_KEY_VARS if env_value(env, name) is not None)
    child["REPOACH_ANTHROPIC_API_KEY"] = env[key_name].strip()
    child.pop("ANTHROPIC_API_KEY", None)
    child["REPOACH_PROXY_HOST"] = LOOPBACK_HOST
    child["REPOACH_PROXY_PORT"] = str(port)
    child["REPOACH_ANTHROPIC_AUTH_TOKEN"] = auth_token
    for tier, default in DEFAULT_MODELS.items():
        override = env_value(env, f"REPOACH_{tier}")
        child[tier] = f"anthropic/{override or default}"
    return child


def _healthy(url: str) -> bool:
    """True when ``GET url`` answers 200; a refused connection is just not yet."""
    try:
        return httpx.get(url, timeout=2.0).status_code == 200
    except httpx.HTTPError as exc:
        _log.debug("ci_gateway_health_pending", url=url, error=type(exc).__name__)
        return False


def _tail(path: Path) -> str:
    """Return the last lines of the gateway log, or a note when it is unreadable."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return f"(gateway log unreadable: {exc})"
    return "\n".join(lines[-LOG_TAIL_LINES:])


@contextmanager
def loopback_gateway(
    env: Mapping[str, str],
    *,
    python: str = sys.executable,
    timeout_s: float = HEALTH_TIMEOUT_S,
    poll_s: float = 0.5,
) -> Iterator[GatewayHandle]:
    """Run ``python -m repoach.llm_proxy`` on the loopback for the ``with`` block.

    Args:
        env: The job environment; must hold one of
            :data:`~repoach.ci_entry.variables.DIRECT_KEY_VARS`.
        python: The interpreter to run the gateway with.
        timeout_s: Deadline for the first healthy ``/health``.
        poll_s: Interval between health probes.

    Yields:
        The gateway's base URL and per-job secret.

    Raises:
        GatewayStartError: The gateway exited or never answered; the
            message carries the tail of its log.
    """
    port_raw = env_value(env, GATEWAY_PORT_VAR)
    port = int(port_raw) if port_raw is not None and port_raw.isdigit() else free_port()
    token = secrets.token_hex(16)
    base_url = f"http://{LOOPBACK_HOST}:{port}"
    with tempfile.TemporaryDirectory(prefix="repoach-ci-gateway-") as workdir:
        log_path = Path(workdir) / "gateway.log"
        with log_path.open("w", encoding="utf-8") as log:
            process = subprocess.Popen(
                [python, "-m", "repoach.llm_proxy"],
                cwd=workdir,
                env=gateway_env(env, port=port, auth_token=token),
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            try:
                deadline = time.monotonic() + timeout_s
                while not _healthy(f"{base_url}/health"):
                    if process.poll() is not None:
                        raise GatewayStartError(
                            f"the gateway exited with {process.returncode} before answering "
                            f"{base_url}/health:\n{_tail(log_path)}"
                        )
                    if time.monotonic() >= deadline:
                        raise GatewayStartError(
                            f"the gateway did not answer {base_url}/health within "
                            f"{timeout_s:.0f}s:\n{_tail(log_path)}"
                        )
                    time.sleep(poll_s)
                _log.info("ci_gateway_up", base_url=base_url)
                yield GatewayHandle(base_url=base_url, auth_token=token)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    _log.warning("ci_gateway_kill", pid=process.pid)
                    process.kill()
                    process.wait()
