"""Which provider a run uses, and whether it can be used (SP-UNIVERSAL-PROVIDERS G3/G4).

``[provider].kind`` (``REPOACH_PROVIDER`` > ``repoach.toml``, resolved by
:func:`~repoach.project.resolve.resolve_project_config`) picks the route:

* ``claude_code`` — the logged-in Claude Code CLI, headless, in-process;
  no gateway, no key.  The CLI is ``[provider].cli_path`` /
  ``REPOACH_CLAUDE_CODE_CLI_PATH``, else ``claude`` on ``PATH``.
* ``anthropic`` — the Messages API in-process with
  ``REPOACH_ANTHROPIC_API_KEY``, else ``ANTHROPIC_API_KEY``.
* anything else (unset, ``gateway``, and the not-yet-routed ``openai`` /
  ``openai_compatible``) — the gateway at ``REPOACH_LLM_PROXY_BASE_URL``,
  exactly as before direct providers existed.

``claude_code`` is never chosen implicitly (G6): only an explicit kind
spends a Claude subscription.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from ..core.config import configured_env_value, get_settings, require_gateway_credentials
from ..core.logging import get_logger
from ..core.paths import project_root
from ..llm.capability import CapabilityTier
from ..llm_proxy.providers.anthropic_messages import AnthropicMessagesTransport
from ..llm_proxy.providers.base import BaseProvider, ProviderConfig
from ..llm_proxy.providers.claude_code import ClaudeCodeProvider
from ..llm_proxy.providers.defaults import ANTHROPIC_DEFAULT_BASE
from ..project.config import ProviderSection
from ..project.resolve import ConfigSource, resolve_project_config
from .direct import DirectProvider
from .errors import ProviderConfigError

_log = get_logger(__name__)

RouteKind = Literal["gateway", "claude_code", "anthropic"]

DIRECT_KINDS: tuple[RouteKind, ...] = ("claude_code", "anthropic")
"""``[provider].kind`` values served in-process, without a gateway."""

ANTHROPIC_CREDENTIAL_NAMES: tuple[str, ...] = ("REPOACH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
"""Environment names the ``anthropic`` provider reads its key from, first set wins."""

ANTHROPIC_BASE_URL_VAR = "REPOACH_ANTHROPIC_BASE_URL"
"""Optional Messages API root (a corporate proxy, a test server); the API's own otherwise."""

DEFAULT_CLI = "claude"

CLAUDE_CODE_MODELS: dict[CapabilityTier, str] = {
    CapabilityTier.OPUS: "opus",
    CapabilityTier.SONNET: "sonnet",
    CapabilityTier.HAIKU: "haiku",
}
"""The CLI's own model aliases per tier."""

ANTHROPIC_MODELS: dict[CapabilityTier, str] = {
    CapabilityTier.OPUS: "claude-opus-5",
    CapabilityTier.SONNET: "claude-sonnet-5",
    CapabilityTier.HAIKU: "claude-haiku-4-5",
}
"""Messages API model per tier; ``ci review``'s loopback gateway uses the same table."""

LOGIN_PROBE_TIMEOUT_S = 30.0

DIRECT_CONCURRENCY = 8
"""Concurrent calls one direct transport admits: four reviewers plus their verifiers."""

CLI_SECRET_SUFFIXES: tuple[str, ...] = ("_API_KEY", "_AUTH_TOKEN", "_TOKEN")
"""Variable-name endings the ``claude`` CLI never inherits (see :func:`claude_cli_env`)."""

CLI_KEPT_SECRETS: frozenset[str] = frozenset({"CLAUDE_CODE_OAUTH_TOKEN"})
"""The one token the CLI keeps: it *is* the login on a headless runner."""


def claude_cli_env(environ: Mapping[str, str]) -> dict[str, str]:
    """Return the environment the ``claude`` CLI runs with: ``environ`` minus every key and token.

    The CLI bills an ``ANTHROPIC_API_KEY`` it finds before the login, so
    a review run with ``kind = "claude_code"`` beside a key (or a gateway
    secret) would silently spend API credit.  Every name ending in
    :data:`CLI_SECRET_SUFFIXES` is dropped, except
    :data:`CLI_KEPT_SECRETS`; everything else (``PATH``, ``HOME``,
    ``LANG``, ``TMPDIR``, ``LD_LIBRARY_PATH``, CA bundles, proxies,
    ``CLAUDE_CONFIG_DIR``) is kept.
    """
    return {
        name: value
        for name, value in environ.items()
        if name in CLI_KEPT_SECRETS or not name.upper().endswith(CLI_SECRET_SUFFIXES)
    }


class ProviderRoute(BaseModel):
    """The provider a run in ``root`` uses.

    Attributes:
        kind: ``gateway``, ``claude_code`` or ``anthropic``.
        configured: ``[provider].kind`` as written (``""`` when unset).
        source: Where ``[provider].kind`` came from.
        section: The resolved ``[provider]`` table.
        root: The project root it was resolved for.
    """

    model_config = ConfigDict(frozen=True)

    kind: RouteKind
    configured: str
    source: ConfigSource
    section: ProviderSection
    root: Path


class ClaudeLogin(BaseModel):
    """What ``claude auth status --json`` says, without the account details.

    Attributes:
        logged_in: The CLI has a usable login or token.
        method: ``authMethod`` (``claude.ai``, ``oauth_token``, ``none``…).
    """

    model_config = ConfigDict(frozen=True)

    logged_in: bool
    method: str


def resolve_route(root: Path | None = None) -> ProviderRoute:
    """Resolve the provider route of the project holding ``root`` (the cwd by default).

    Raises:
        ConfigError: ``repoach.toml`` or a ``REPOACH_*`` value is invalid.
    """
    project = project_root(root)
    resolved = resolve_project_config(project)
    section = resolved.config.provider
    kind: RouteKind = "gateway"
    if section.kind == "claude_code":
        kind = "claude_code"
    elif section.kind == "anthropic":
        kind = "anthropic"
    return ProviderRoute(
        kind=kind,
        configured=section.kind,
        source=resolved.source("provider.kind"),
        section=section,
        root=project,
    )


def tier_models(route: ProviderRoute) -> dict[CapabilityTier, str]:
    """Return the model per tier: ``[provider].<tier>`` / ``REPOACH_MODEL_<TIER>``, else the default."""
    defaults = CLAUDE_CODE_MODELS if route.kind == "claude_code" else ANTHROPIC_MODELS
    overrides = {
        CapabilityTier.OPUS: route.section.opus,
        CapabilityTier.SONNET: route.section.sonnet,
        CapabilityTier.HAIKU: route.section.haiku,
    }
    return {tier: overrides[tier].strip() or default for tier, default in defaults.items()}


def claude_cli(route: ProviderRoute) -> str:
    """Return the absolute path of the ``claude`` executable the route runs.

    Raises:
        ProviderConfigError: Not found; the message names where it was looked for.
    """
    wanted = route.section.cli_path.strip() or DEFAULT_CLI
    found = shutil.which(wanted)
    if found is None:
        raise ProviderConfigError(
            f"provider claude_code: claude CLI not found ({wanted!r}); install Claude Code "
            "and log in, or set REPOACH_CLAUDE_CODE_CLI_PATH / [provider].cli_path"
        )
    return found


def claude_login(cli: str) -> ClaudeLogin:
    """Ask the CLI whether it is logged in (``claude auth status --json``).

    Only ``loggedIn`` and ``authMethod`` are read; the account e-mail and
    organisation the command also prints are never kept.

    Raises:
        ProviderConfigError: The command failed to run or printed no JSON
            (a Claude Code too old to have ``auth status``).
    """
    try:
        completed = subprocess.run(
            [cli, "auth", "status", "--json"],
            env=claude_cli_env(os.environ),
            capture_output=True,
            text=True,
            timeout=LOGIN_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderConfigError(
            f"provider claude_code: '{cli} auth status' did not run: {exc}"
        ) from exc
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        head = (completed.stdout or completed.stderr).strip()[:200]
        raise ProviderConfigError(
            f"provider claude_code: '{cli} auth status --json' printed no JSON "
            f"(rc {completed.returncode}: {head!r}); update Claude Code"
        ) from exc
    if not isinstance(payload, dict):
        raise ProviderConfigError(
            f"provider claude_code: '{cli} auth status --json' printed {type(payload).__name__}"
        )
    return ClaudeLogin(
        logged_in=payload.get("loggedIn") is True, method=str(payload.get("authMethod") or "none")
    )


def anthropic_credential(root: Path) -> tuple[str, str] | None:
    """Return ``(name, value)`` of the first Anthropic key set, else None.

    Reads :data:`ANTHROPIC_CREDENTIAL_NAMES` in order through every
    environment layer (process, ``~/.config/repoach/env``…).
    """
    for name in ANTHROPIC_CREDENTIAL_NAMES:
        value = configured_env_value((name,), root)
        if value is not None:
            return name, value.strip()
    return None


def _missing_anthropic_key() -> ProviderConfigError:
    names = ", then ".join(ANTHROPIC_CREDENTIAL_NAMES)
    return ProviderConfigError(
        f"provider anthropic: no API key -- set {names} in the environment or ~/.config/repoach/env"
    )


def build_direct_provider(route: ProviderRoute, *, timeout_s: float) -> DirectProvider:
    """Build the in-process provider for a ``claude_code`` or ``anthropic`` route.

    Checks what can be checked without a call (the CLI exists, the key
    is set); the login itself is checked by :func:`require_provider_ready`.

    Args:
        route: A route whose kind is in :data:`DIRECT_KINDS`.
        timeout_s: Per-call budget (the CLI's subprocess timeout, the
            HTTP read timeout).

    Raises:
        ProviderConfigError: The CLI or the key is missing.
        ValueError: ``route`` is the gateway route.
    """
    models = tier_models(route)
    if route.kind == "claude_code":
        cli = claude_cli(route)

        def claude_transport() -> BaseProvider:
            return ClaudeCodeProvider(
                ProviderConfig(api_key="", max_concurrency=DIRECT_CONCURRENCY),
                cli_path=cli,
                default_model=CLAUDE_CODE_MODELS[CapabilityTier.SONNET],
                subprocess_timeout=timeout_s,
                own_process_group=True,
                child_env=claude_cli_env,
            )

        return DirectProvider(
            name="claude_code",
            models=models,
            transport_key=("claude_code", cli, str(timeout_s)),
            transport_factory=claude_transport,
            timeout_s=timeout_s,
            auth_hint=f"run '{cli} auth login' (or set CLAUDE_CODE_OAUTH_TOKEN from 'claude setup-token')",
        )
    if route.kind == "anthropic":
        credential = anthropic_credential(route.root)
        if credential is None:
            raise _missing_anthropic_key()
        name, key = credential
        base_url = (
            configured_env_value((ANTHROPIC_BASE_URL_VAR,), route.root) or ANTHROPIC_DEFAULT_BASE
        )

        def anthropic_transport() -> BaseProvider:
            return AnthropicMessagesTransport(
                ProviderConfig(
                    api_key=key,
                    base_url=base_url,
                    max_concurrency=DIRECT_CONCURRENCY,
                    http_read_timeout=timeout_s,
                ),
                provider_name="anthropic",
                default_base_url=ANTHROPIC_DEFAULT_BASE,
            )

        return DirectProvider(
            name="anthropic",
            models=models,
            transport_key=("anthropic", base_url, name, key, str(timeout_s)),
            transport_factory=anthropic_transport,
            timeout_s=timeout_s,
            auth_hint=f"the key in {name} was rejected; replace it",
        )
    raise ValueError(f"route {route.kind!r} is not a direct provider")


def require_provider_ready(root: Path | None = None) -> ProviderRoute:
    """Check, before any forge call, that the selected provider can answer.

    * gateway: the URL and its shared secret
      (:func:`~repoach.core.config.require_gateway_credentials`, unchanged);
    * ``claude_code``: the CLI exists and ``auth status`` says logged in;
    * ``anthropic``: a key is set.

    Returns:
        The route that was checked.

    Raises:
        GatewayConfigError: The gateway is not configured.
        ProviderConfigError: The direct provider cannot be used; one line
            naming what to install, run or set (never a value).
    """
    route = resolve_route(root)
    if route.kind == "gateway":
        require_gateway_credentials(get_settings())
        return route
    if route.kind == "claude_code":
        cli = claude_cli(route)
        login = claude_login(cli)
        if not login.logged_in:
            raise ProviderConfigError(
                f"provider claude_code: {cli} is not logged in -- run '{cli} auth login' "
                "(or set CLAUDE_CODE_OAUTH_TOKEN from 'claude setup-token')"
            )
        _log.info("provider_ready", provider="claude_code", auth_method=login.method)
        return route
    if anthropic_credential(route.root) is None:
        raise _missing_anthropic_key()
    _log.info("provider_ready", provider="anthropic")
    return route


__all__ = [
    "ANTHROPIC_CREDENTIAL_NAMES",
    "ANTHROPIC_MODELS",
    "CLAUDE_CODE_MODELS",
    "DIRECT_KINDS",
    "ClaudeLogin",
    "ProviderRoute",
    "anthropic_credential",
    "build_direct_provider",
    "claude_cli",
    "claude_cli_env",
    "claude_login",
    "require_provider_ready",
    "resolve_route",
    "tier_models",
]
