"""Ferova LLM proxy entrypoint.

Run with::

    python -m repoach.llm_proxy

The host / port / .env are picked up from the Ferova root .env
file via :class:`Settings`. The proxy speaks the Anthropic Messages
protocol on the configured port (default 8082) and routes calls to
the upstream provider implied by the model name (NVIDIA NIM /
OpenRouter / claude_code).
"""

import uvicorn

from .api.app import app, create_app
from .config.settings import get_settings

__all__ = ["app", "create_app"]


def main() -> None:
    """Run the proxy server with graceful shutdown.

    Mirrors vendor's server.py: ``timeout_graceful_shutdown=5`` so uvicorn
    doesn't hang on task cleanup, and :func:`kill_all_best_effort` runs
    in ``finally`` to clean any subprocess (e.g. the claude_code provider's
    CLI child) that lifespan shutdown may not have terminated.
    """
    from .cli.process_registry import kill_all_best_effort

    settings = get_settings()
    try:
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level="info",
            timeout_graceful_shutdown=5,
        )
    finally:
        kill_all_best_effort()


if __name__ == "__main__":
    main()
