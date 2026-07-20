"""Application runtime composition and lifecycle ownership."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi import FastAPI
from loguru import logger

from repoach.llm_proxy.config.settings import Settings, get_settings
from repoach.llm_proxy.providers.registry import ProviderRegistry

_SHUTDOWN_TIMEOUT_S = 5.0


async def best_effort(name: str, awaitable: Any, timeout_s: float = _SHUTDOWN_TIMEOUT_S) -> None:
    """Run a shutdown step with timeout; never raise to callers."""
    try:
        await asyncio.wait_for(awaitable, timeout=timeout_s)
    except TimeoutError:
        logger.warning(f"Shutdown step timed out: {name} ({timeout_s}s)")
    except Exception as e:
        logger.warning(f"Shutdown step failed: {name}: {type(e).__name__}: {e}")


def warn_if_process_auth_token(settings: Settings) -> None:
    """Warn when server auth was implicitly inherited from the shell."""
    if settings.uses_process_anthropic_auth_token():
        logger.warning(
            "ANTHROPIC_AUTH_TOKEN is set in the process environment but not in "
            "a configured .env file. The proxy will require that token. Add "
            "ANTHROPIC_AUTH_TOKEN= to .env to disable proxy auth, or set the "
            "same token in .env to make server auth explicit."
        )


@dataclass(slots=True)
class AppRuntime:
    """Own the provider registry and its lifecycle."""

    app: FastAPI
    settings: Settings
    _provider_registry: ProviderRegistry | None = field(default=None, init=False)

    @classmethod
    def for_app(
        cls,
        app: FastAPI,
        settings: Settings | None = None,
    ) -> AppRuntime:
        return cls(app=app, settings=settings or get_settings())

    async def startup(self) -> None:
        logger.info("Starting Claude Code Proxy...")
        self._provider_registry = ProviderRegistry()
        self.app.state.provider_registry = self._provider_registry
        warn_if_process_auth_token(self.settings)
        self._seed_breaker_from_probes()
        self._seed_effort_map()

    def _seed_effort_map(self) -> None:
        """Seed the resolved-effort map from recent probe history (best-effort).

        Gated by ``effort_map_seed_enabled``. Any failure (missing or unreadable
        DB, empty history) is logged and swallowed — seeding is an optimisation
        and must never block the proxy from starting. Mirrors
        :meth:`_seed_breaker_from_probes`.
        """
        if not self.settings.effort_map_seed_enabled:
            return
        from pathlib import Path

        from repoach.llm_proxy.providers.effort_map import seed_effort_map

        try:
            wired = seed_effort_map(db_path=Path(self.settings.breaker_probe_seed_db))
            logger.info("Effort map seeded from probe history: {} cell(s) wired", wired)
        except Exception as exc:
            logger.warning("Effort-map seed skipped: {}", exc)

    def _seed_breaker_from_probes(self) -> None:
        """Pre-trip the breaker from recent probe history (best-effort).

        Gated by ``breaker_probe_seed_enabled``. Any failure (missing or
        unreadable DB, empty history) is logged and swallowed — seeding is
        an optimisation and must never block the proxy from starting.
        """
        if not self.settings.breaker_probe_seed_enabled:
            return
        import time
        from pathlib import Path

        from repoach.llm_proxy.routing.probe_seed import seed_breaker_from_probes

        try:
            tripped = seed_breaker_from_probes(
                self.settings,
                now=time.monotonic(),
                db_path=Path(self.settings.breaker_probe_seed_db),
            )
            logger.info("Breaker seeded from probe history: {} tier head(s) tripped", tripped)
        except Exception as exc:
            logger.warning("Breaker probe-seed skipped: {}", exc)

    async def shutdown(self) -> None:
        logger.info("Shutdown requested, cleaning up...")
        if self._provider_registry is not None:
            await best_effort("provider_registry.cleanup", self._provider_registry.cleanup())
        logger.info("Server shut down cleanly")
