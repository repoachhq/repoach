"""Ferova local LLM proxy.

Ingested from ``lsdefine/free-claude-code`` and trimmed to the
serving path only (messaging platforms and the interactive CLI are
dropped). The proxy speaks the Anthropic Messages protocol on
``http://localhost:8082`` and routes to NVIDIA NIM, OpenRouter, or
claude_code depending on the configured model slot (``MODEL_OPUS`` /
``MODEL_SONNET`` / ``MODEL_HAIKU``).

This package init is deliberately empty.  Importing ``app`` /
``create_app`` here used to be a convenience re-export, but that
forced loading :mod:`ferova.llm_proxy.api.app` for every child
module imported elsewhere (e.g. ``llm_proxy.api.models.agent_v1``
pulled in by the WA harness).  Loading ``api.app`` runs
:func:`configure_logging` at module level, which replaces
``logging.root.handlers`` with the loguru :class:`InterceptHandler`
and routes every stdlib log line into ``server.log`` instead of
stdout.  The scheduler / sentinel / webhook services therefore had
their structlog output silently rerouted away from
``logs/scheduler.log`` (and friends) — see SP-SCHEDULER-LOG-ROUTE
for the full incident.  The proxy still configures its own logging
inside :mod:`__main__` and :mod:`api.app` themselves, which is the
correct scope.
"""
