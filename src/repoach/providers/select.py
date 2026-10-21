"""Which provider a run uses, and whether it can be used (SP-UNIVERSAL-PROVIDERS G3/G4).

The provider name is resolved ``--provider`` (:func:`set_provider_override`)
> ``REPOACH_PROVIDER`` > ``[provider].kind`` in ``repoach.toml`` (the last
two by :func:`~repoach.project.resolve.resolve_project_config`) and picks
the route:

* ``claude_code`` — the logged-in Claude Code CLI, headless, in-process;
  no gateway, no key.  The CLI is ``[provider].cli_path`` /
  ``REPOACH_CLAUDE_CODE_CLI_PATH``, else ``claude`` on ``PATH``.
* ``anthropic`` — the Messages API in-process with
  ``REPOACH_ANTHROPIC_API_KEY``, else ``ANTHROPIC_API_KEY``.
* ``openai_compatible`` — any ``/chat/completions`` endpoint in-process
  at ``[provider].base_url`` / ``REPOACH_OPENAI_BASE_URL``, keyed by
  ``REPOACH_OPENAI_API_KEY``, else ``OPENAI_API_KEY`` (optional: a local
  server takes none).  A preset name (:data:`PRESETS`: ``openai``,
  ``ollama``, ``openrouter``, ``groq``, ``deepseek``, ``mistral``) is the
  same route with the base URL and the key names filled in.
* ``gemini`` — Google's Generative Language API in-process, through its
  OpenAI-compatible surface (``/v1beta/openai``), keyed by
  ``REPOACH_GEMINI_API_KEY``, else ``GEMINI_API_KEY``, else
  ``GOOGLE_API_KEY``.  Gemini through OpenRouter is the ``openrouter``
  preset with a ``google/gemini-…`` model id.
* ``gateway`` — the chain-failover gateway at ``REPOACH_LLM_PROXY_BASE_URL``,
  exactly as before direct providers existed; also the route when no
  name is set but a gateway URL is (the pre-provider configuration).

Nothing named and no gateway URL is :class:`NoProviderConfiguredError`.
``claude_code`` is never chosen implicitly (G6): only an explicit name
spends a Claude subscription.  The ``openai_compatible`` and ``gemini``
routes have no default model: each tier is ``[provider].<tier>`` /
``REPOACH_MODEL_<TIER>``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict

from ..core.config import configured_env_value, get_settings, require_gateway_credentials
from ..core.logging import get_logger
from ..core.paths import project_root
from ..llm.capability import CapabilityTier
from ..llm_proxy.providers.anthropic_messages import AnthropicMessagesTransport
from ..llm_proxy.providers.base import BaseProvider, ProviderConfig
from ..llm_proxy.providers.claude_code import ClaudeCodeProvider
from ..llm_proxy.providers.defaults import (
    ANTHROPIC_DEFAULT_BASE,
    DEEPSEEK_DEFAULT_BASE,
    GROQ_DEFAULT_BASE,
    OPENAI_DEFAULT_BASE,
    OPENROUTER_DEFAULT_BASE,
)
from ..project.config import ProviderSection
from ..project.resolve import ConfigSource, resolve_project_config
from .direct import DirectProvider, MeteredChatTransport
from .errors import NoProviderConfiguredError, ProviderConfigError

_log = get_logger(__name__)

RouteKind = Literal["gateway", "claude_code", "anthropic", "openai_compatible", "gemini"]
RouteSource = ConfigSource | Literal["flag"]

DIRECT_KINDS: tuple[RouteKind, ...] = ("claude_code", "anthropic", "openai_compatible", "gemini")
"""Routes served in-process, without a gateway."""

KINDS: tuple[str, ...] = ("claude_code", "anthropic", "openai_compatible", "gemini", "gateway")
"""The provider kinds, in the order the no-provider message lists them."""

ANTHROPIC_CREDENTIAL_NAMES: tuple[str, ...] = ("REPOACH_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY")
"""Environment names the ``anthropic`` provider reads its key from, first set wins."""

ANTHROPIC_BASE_URL_VAR = "REPOACH_ANTHROPIC_BASE_URL"
"""Optional Messages API root (a corporate proxy, a test server); the API's own otherwise."""

OPENAI_COMPATIBLE_CREDENTIAL_NAMES: tuple[str, ...] = ("REPOACH_OPENAI_API_KEY", "OPENAI_API_KEY")
"""Key names of a bare ``openai_compatible`` route and of the ``openai`` preset."""

GEMINI_CREDENTIAL_NAMES: tuple[str, ...] = (
    "REPOACH_GEMINI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
)
"""Environment names the ``gemini`` provider reads its key from, first set wins."""

GEMINI_DEFAULT_BASE = "https://generativelanguage.googleapis.com/v1beta/openai"
"""The Generative Language API's OpenAI-compatible root (ai.google.dev/gemini-api/docs/openai, read 2026-10-21)."""

KEYLESS_API_KEY = "no-key"
"""Sent as the bearer token when no key is set: the OpenAI SDK refuses an empty
key, and a keyless server (Ollama, vLLM, llama.cpp) ignores the header — Ollama's
own documentation passes a placeholder the same way."""

OLLAMA_DEFAULT_BASE = "http://localhost:11434/v1"
MISTRAL_DEFAULT_BASE = "https://api.mistral.ai/v1"


class Preset(BaseModel):
    """A named ``openai_compatible`` endpoint: a base URL and the key names, nothing else.

    Attributes:
        base_url: The endpoint root ``/chat/completions`` is posted under.
        credential_names: Environment names the key is read from, first
            set wins; empty for a server that takes no key.
    """

    model_config = ConfigDict(frozen=True)

    base_url: str
    credential_names: tuple[str, ...]


PRESETS: dict[str, Preset] = {
    "openai": Preset(
        base_url=OPENAI_DEFAULT_BASE, credential_names=OPENAI_COMPATIBLE_CREDENTIAL_NAMES
    ),
    "ollama": Preset(base_url=OLLAMA_DEFAULT_BASE, credential_names=()),
    "openrouter": Preset(
        base_url=OPENROUTER_DEFAULT_BASE,
        credential_names=("REPOACH_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"),
    ),
    "groq": Preset(
        base_url=GROQ_DEFAULT_BASE, credential_names=("REPOACH_GROQ_API_KEY", "GROQ_API_KEY")
    ),
    "deepseek": Preset(
        base_url=DEEPSEEK_DEFAULT_BASE,
        credential_names=("REPOACH_DEEPSEEK_API_KEY", "DEEPSEEK_API_KEY"),
    ),
    "mistral": Preset(
        base_url=MISTRAL_DEFAULT_BASE,
        credential_names=("REPOACH_MISTRAL_API_KEY", "MISTRAL_API_KEY"),
    ),
}
"""``--provider`` / ``[provider].kind`` names that mean ``openai_compatible`` at a known endpoint."""

SELECTABLE: tuple[str, ...] = KINDS + tuple(PRESETS)
"""Every value ``--provider``, ``REPOACH_PROVIDER`` and ``[provider].kind`` accept."""

PROVIDER_FLAG_HELP = (
    "Provider for this run, outranking REPOACH_PROVIDER and [provider].kind: "
    "claude_code, anthropic, openai_compatible, gemini, gateway, or a preset "
    "(openai, ollama, openrouter, groq, deepseek, mistral)."
)
"""``--provider``'s help text, shared by every command that takes the flag."""

_VERSION_SEGMENT = re.compile(r"^v\d+[a-z0-9]*$")

_OVERRIDE: str | None = None
"""The ``--provider`` value of this process, None when the flag was not given."""


def set_provider_override(value: str | None) -> None:
    """Record ``--provider`` for every route resolved in this process (flag > env > file).

    Args:
        value: A name from :data:`SELECTABLE`, or None to clear.

    Raises:
        ProviderConfigError: ``value`` names no provider; the message
            lists the accepted names.
    """
    global _OVERRIDE
    if value is not None and value not in SELECTABLE:
        raise ProviderConfigError(
            f"unknown provider {value!r} -- use one of {', '.join(SELECTABLE)}"
        )
    _OVERRIDE = value


def provider_override() -> str | None:
    """Return the ``--provider`` value of this process, None when not given."""
    return _OVERRIDE


def anthropic_base_url(raw: str | None) -> str:
    """Return the Messages API root the transport posts ``/messages`` under.

    ``https://proxy.example`` and ``https://proxy.example/v1`` both mean
    the same API (the form ``ANTHROPIC_BASE_URL`` users write has no
    ``/v1``); the transport appends ``/messages``, so a root whose path
    does not already end in ``/v1`` gets it, and the request lands on
    ``/v1/messages``, never on the 404 ``/messages``.

    Args:
        raw: ``REPOACH_ANTHROPIC_BASE_URL``, None when unset.

    Returns:
        :data:`ANTHROPIC_DEFAULT_BASE` when ``raw`` is unset or blank, else
        ``raw`` without a trailing slash, ending in ``/v1``.
    """
    if raw is None or not raw.strip():
        return ANTHROPIC_DEFAULT_BASE
    base = raw.strip().rstrip("/")
    return base if base.endswith("/v1") else f"{base}/v1"


def openai_compatible_base_url(raw: str) -> str:
    """Return the root ``/chat/completions`` is posted under.

    Users write an Ollama or vLLM server as ``http://host:11434``; the
    API lives under ``/v1``.  A root whose path has no version segment
    (``v1``, ``v1beta``, ``v4``…) gets ``/v1``; one that has it
    (``https://api.groq.com/openai/v1``,
    ``https://generativelanguage.googleapis.com/v1beta/openai``) is kept.

    Args:
        raw: The configured root.

    Returns:
        ``raw`` without a trailing slash, with ``/v1`` appended when its
        path names no version.
    """
    base = raw.strip().rstrip("/")
    segments = urlparse(base).path.split("/")
    if any(_VERSION_SEGMENT.match(segment) for segment in segments):
        return base
    return f"{base}/v1"


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

DEFAULT_MODELS: dict[str, dict[CapabilityTier, str]] = {
    "claude_code": CLAUDE_CODE_MODELS,
    "anthropic": ANTHROPIC_MODELS,
}
"""Built-in tier models per route; a route absent here has none (every tier must be set)."""

TIER_SETTINGS: dict[CapabilityTier, str] = {
    CapabilityTier.OPUS: "REPOACH_MODEL_OPUS / [provider].opus",
    CapabilityTier.SONNET: "REPOACH_MODEL_SONNET / [provider].sonnet",
    CapabilityTier.HAIKU: "REPOACH_MODEL_HAIKU / [provider].haiku",
}

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
        kind: ``gateway``, ``claude_code``, ``anthropic``,
            ``openai_compatible`` or ``gemini``.
        configured: The provider name as given (``""`` when none was).
        preset: The :data:`PRESETS` name when ``configured`` is one, else ``""``.
        source: Where ``configured`` came from (``flag``, ``env``, ``file``,
            ``default``).
        section: The resolved ``[provider]`` table.
        root: The project root it was resolved for.
    """

    model_config = ConfigDict(frozen=True)

    kind: RouteKind
    configured: str
    preset: str = ""
    source: RouteSource
    section: ProviderSection
    root: Path

    @property
    def label(self) -> str:
        """The provider name usage rows and ``model_used`` carry: the preset, else the kind."""
        return self.preset or self.kind


class ClaudeLogin(BaseModel):
    """What ``claude auth status --json`` says, without the account details.

    Attributes:
        logged_in: The CLI has a usable login or token.
        method: ``authMethod`` (``claude.ai``, ``oauth_token``, ``none``…).
    """

    model_config = ConfigDict(frozen=True)

    logged_in: bool
    method: str


def _route_kind(name: str) -> tuple[RouteKind, str]:
    """Return ``(route kind, preset)`` for a provider name (unset or unknown: the gateway)."""
    if name in PRESETS:
        return "openai_compatible", name
    if name in ("claude_code", "anthropic", "openai_compatible", "gemini"):
        return name, ""
    return "gateway", ""


def resolve_route(root: Path | None = None) -> ProviderRoute:
    """Resolve the provider route of the project holding ``root`` (the cwd by default).

    Raises:
        ConfigError: ``repoach.toml`` or a ``REPOACH_*`` value is invalid.
    """
    project = project_root(root)
    resolved = resolve_project_config(project)
    section = resolved.config.provider
    source: RouteSource = resolved.source("provider.kind")
    configured: str = section.kind
    if _OVERRIDE is not None:
        configured, source = _OVERRIDE, "flag"
    kind, preset = _route_kind(configured)
    return ProviderRoute(
        kind=kind,
        configured=configured,
        preset=preset,
        source=source,
        section=section,
        root=project,
    )


def tier_models(route: ProviderRoute) -> dict[CapabilityTier, str]:
    """Return the model per tier: ``[provider].<tier>`` / ``REPOACH_MODEL_<TIER>``, else the default.

    Raises:
        ProviderConfigError: The route has no default model and a tier is
            unset; the message names every missing setting.
    """
    defaults = DEFAULT_MODELS.get(route.kind, {})
    overrides = {
        CapabilityTier.OPUS: route.section.opus,
        CapabilityTier.SONNET: route.section.sonnet,
        CapabilityTier.HAIKU: route.section.haiku,
    }
    models: dict[CapabilityTier, str] = {}
    missing: list[str] = []
    for tier in TIER_SETTINGS:
        value = overrides[tier].strip() or defaults.get(tier, "")
        if value:
            models[tier] = value
        else:
            missing.append(TIER_SETTINGS[tier])
    if missing:
        raise ProviderConfigError(
            f"provider {route.label}: no model for {len(missing)} tier(s) -- set "
            f"{', '.join(missing)} (this provider has no default model)"
        )
    return models


def provider_credential_names(name: str) -> tuple[str, ...]:
    """Return the environment names provider ``name`` reads its key from, first set wins.

    Empty for ``claude_code`` (its login is the CLI's), ``gateway`` (its
    secret is :func:`~repoach.core.config.require_gateway_credentials`'s)
    and a keyless preset (``ollama``).

    Args:
        name: A kind or a preset (:data:`SELECTABLE`).
    """
    kind, preset = _route_kind(name)
    if kind == "anthropic":
        return ANTHROPIC_CREDENTIAL_NAMES
    if kind == "gemini":
        return GEMINI_CREDENTIAL_NAMES
    if kind == "openai_compatible":
        if preset:
            return PRESETS[preset].credential_names
        return OPENAI_COMPATIBLE_CREDENTIAL_NAMES
    return ()


def provider_needs_key(name: str) -> bool:
    """True when provider ``name`` cannot be called without a key.

    A bare ``openai_compatible`` route takes an optional key (a local
    vLLM or llama.cpp server takes none); every keyed preset, ``gemini``
    and ``anthropic`` need one.
    """
    if name == "openai_compatible":
        return False
    return bool(provider_credential_names(name))


def credential_names(route: ProviderRoute) -> tuple[str, ...]:
    """Return the environment names ``route`` reads its key from (:func:`provider_credential_names`)."""
    return provider_credential_names(route.preset or route.kind)


def credential_required(route: ProviderRoute) -> bool:
    """True when ``route`` cannot be called without a key (:func:`provider_needs_key`)."""
    return provider_needs_key(route.preset or route.kind)


def first_credential(names: tuple[str, ...], root: Path) -> tuple[str, str] | None:
    """Return ``(name, value)`` of the first of ``names`` set in any environment layer, else None."""
    for name in names:
        value = configured_env_value((name,), root)
        if value is not None and value.strip():
            return name, value.strip()
    return None


def route_base_url(route: ProviderRoute) -> str:
    """Return the root an ``openai_compatible`` or ``gemini`` route posts to.

    ``[provider].base_url`` / ``REPOACH_OPENAI_BASE_URL`` wins over the
    preset's or Gemini's own root.

    Raises:
        ProviderConfigError: A bare ``openai_compatible`` route with no
            base URL.
    """
    configured = route.section.base_url.strip()
    if route.kind == "gemini":
        return (configured or GEMINI_DEFAULT_BASE).rstrip("/")
    if configured:
        return openai_compatible_base_url(configured)
    if route.preset:
        return PRESETS[route.preset].base_url
    raise ProviderConfigError(
        "provider openai_compatible: no base URL -- set [provider].base_url in repoach.toml "
        "or REPOACH_OPENAI_BASE_URL (or name a preset: " + ", ".join(PRESETS) + ")"
    )


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
    return first_credential(ANTHROPIC_CREDENTIAL_NAMES, root)


def _missing_key(route: ProviderRoute) -> ProviderConfigError:
    names = ", then ".join(credential_names(route))
    return ProviderConfigError(
        f"provider {route.label}: no API key -- set {names} in the environment or "
        "~/.config/repoach/env"
    )


def _chat_provider(route: ProviderRoute, *, timeout_s: float) -> DirectProvider:
    """Build the in-process ``/chat/completions`` provider of an ``openai_compatible`` or ``gemini`` route."""
    models = tier_models(route)
    base_url = route_base_url(route)
    credential = first_credential(credential_names(route), route.root)
    if credential is None and credential_required(route):
        raise _missing_key(route)
    name, key = credential if credential is not None else ("", KEYLESS_API_KEY)
    label = route.label

    def chat_transport() -> BaseProvider:
        return MeteredChatTransport(
            ProviderConfig(
                api_key=key,
                base_url=base_url,
                max_concurrency=DIRECT_CONCURRENCY,
                http_read_timeout=timeout_s,
            ),
            provider_name=label,
        )

    if name:
        hint = f"the key in {name} was rejected by {base_url}; replace it"
    else:
        hint = (
            f"{base_url} refused a request without a key -- set "
            f"{', then '.join(credential_names(route) or OPENAI_COMPATIBLE_CREDENTIAL_NAMES)}"
        )
    return DirectProvider(
        name=label,
        models=models,
        transport_key=(label, base_url, name, key, str(timeout_s)),
        transport_factory=chat_transport,
        timeout_s=timeout_s,
        auth_hint=hint,
    )


def build_direct_provider(route: ProviderRoute, *, timeout_s: float) -> DirectProvider:
    """Build the in-process provider of a route in :data:`DIRECT_KINDS`.

    Checks what can be checked without a call (the CLI exists, the key
    is set, every tier has a model, a base URL is known); the
    ``claude_code`` login itself is checked by :func:`require_provider_ready`.

    Args:
        route: A route whose kind is in :data:`DIRECT_KINDS`.
        timeout_s: Per-call budget (the CLI's subprocess timeout, the
            HTTP read timeout).

    Raises:
        ProviderConfigError: The CLI, the key, a tier model or the base
            URL is missing.
        ValueError: ``route`` is the gateway route.
    """
    if route.kind in ("openai_compatible", "gemini"):
        return _chat_provider(route, timeout_s=timeout_s)
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
            raise _missing_key(route)
        name, key = credential
        base_url = anthropic_base_url(configured_env_value((ANTHROPIC_BASE_URL_VAR,), route.root))

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


def require_configured(route: ProviderRoute, gateway_url: str | None) -> None:
    """Raise the one no-provider error when nothing names a provider and no gateway URL is set.

    Args:
        route: The resolved route.
        gateway_url: ``REPOACH_LLM_PROXY_BASE_URL`` as resolved, None or
            empty when unset.

    Raises:
        NoProviderConfiguredError: ``route.configured`` is empty and
            ``gateway_url`` is not set.
    """
    if not route.configured and not gateway_url:
        raise NoProviderConfiguredError()


def require_provider_ready(root: Path | None = None) -> ProviderRoute:
    """Check, before any forge call, that the selected provider can answer.

    * nothing named and no gateway URL: :class:`NoProviderConfiguredError`;
    * gateway: the URL and its shared secret
      (:func:`~repoach.core.config.require_gateway_credentials`, unchanged);
    * ``claude_code``: the CLI exists and ``auth status`` says logged in;
    * ``anthropic``, ``openai_compatible``, ``gemini``: the key (when the
      route needs one), every tier model and the base URL are set.

    Returns:
        The route that was checked.

    Raises:
        GatewayConfigError: The gateway is not configured.
        ProviderConfigError: The provider cannot be used; one line naming
            what to install, run or set (never a value).
    """
    route = resolve_route(root)
    if route.kind == "gateway":
        settings = get_settings()
        require_configured(route, settings.llm_proxy_base_url)
        require_gateway_credentials(settings)
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
    build_direct_provider(route, timeout_s=LOGIN_PROBE_TIMEOUT_S)
    _log.info("provider_ready", provider=route.label)
    return route


__all__ = [
    "ANTHROPIC_CREDENTIAL_NAMES",
    "ANTHROPIC_MODELS",
    "CLAUDE_CODE_MODELS",
    "DIRECT_KINDS",
    "GEMINI_CREDENTIAL_NAMES",
    "GEMINI_DEFAULT_BASE",
    "KEYLESS_API_KEY",
    "KINDS",
    "OPENAI_COMPATIBLE_CREDENTIAL_NAMES",
    "PRESETS",
    "PROVIDER_FLAG_HELP",
    "SELECTABLE",
    "ClaudeLogin",
    "Preset",
    "ProviderRoute",
    "anthropic_base_url",
    "anthropic_credential",
    "build_direct_provider",
    "claude_cli",
    "claude_cli_env",
    "claude_login",
    "credential_names",
    "credential_required",
    "first_credential",
    "openai_compatible_base_url",
    "provider_credential_names",
    "provider_needs_key",
    "provider_override",
    "require_configured",
    "require_provider_ready",
    "resolve_route",
    "route_base_url",
    "set_provider_override",
    "tier_models",
]
