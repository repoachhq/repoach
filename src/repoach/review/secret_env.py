"""Secret-bearing env scrub for subprocesses that execute agent-authored code.

A coding agent (DEVAGENT Developer / Coder) authors test files the runner then
*executes* (pytest imports ``conftest.py`` and the target module, so arbitrary
agent-written code runs). The filesystem write-jail cannot contain that, so every
subprocess that runs such code must be denied the operator's live credentials:
this strips every environment variable whose name carries a secret marker
(``*TOKEN*`` / ``*KEY*`` / ``*SECRET*`` / ``*PASSWORD*`` / ``*PASSWD*`` /
``*CREDENTIAL*`` / ``*AUTH*`` / ``*BEARER*`` / ``*DATABASE_URL*``) while leaving
the non-secret config (``PATH``, ``PYTHONPATH``, ``REPOACH_DB_PATH``,
``REPOACH_CODER_PYTHONS``, …) the tests need.

It lives in its own leaf module (imports only :mod:`os`) so both the in-loop tools
(:mod:`review.devagent_tools`) and the runner's authoritative reruns
(:func:`review.dev_runner.run_pytest_selectors`, :func:`review.coder_loop.run_pytest`)
can share one implementation without an import cycle (SP-DEVAGENT-LOOP, finding S4:
the in-loop scrub is illusory if the authoritative reruns of the same code keep the
full environment). SP-SECRET-ENV-UNIFY made this the single scrubber repo-wide:
the delegated-exploration ``claude -p`` child spawned by
:func:`review.planner_cc.run_cc_exploration` now routes through
:func:`scrubbed_env` too, instead of its own prefix-only filter that let
unprefixed provider keys and ``GITHUB_TOKEN``/``GH_TOKEN`` reach a subprocess
that reads untrusted repository content.
"""

from __future__ import annotations

import os

_SECRET_ENV_MARKERS: tuple[str, ...] = (
    "TOKEN",
    "KEY",
    "SECRET",
    "PASSWORD",
    "PASSWD",
    "CREDENTIAL",
    "AUTH",
    "BEARER",
    "DATABASE_URL",
)


def scrubbed_env() -> dict[str, str]:
    """The current environment with secret-bearing variables removed.

    Returns:
        A copy of ``os.environ`` minus every key whose upper-cased name contains a
        secret marker, suitable as the ``env=`` of a subprocess that runs
        agent-authored code.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not any(marker in key.upper() for marker in _SECRET_ENV_MARKERS)
    }


__all__ = ["scrubbed_env"]
