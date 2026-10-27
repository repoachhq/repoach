"""Per-job proxy identity token (SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB).

Two review jobs on two self-hosted runners of the same host used to start
their proxy on the same hardcoded port; the second job's ``/health`` poll
was answered by the first job's proxy. ``scripts/job_proxy.sh`` now gives
each job its own port and proves ownership by PID first. This module is
the second proof: the job passes a token in ``REPOACH_PROXY_INSTANCE`` and
``/health`` echoes it, so the job can also check the token when the proxy
is recent enough to answer one.
"""

from __future__ import annotations

import os

INSTANCE_ENV = "REPOACH_PROXY_INSTANCE"
"""Env var carrying the token the job generated for its own proxy."""


def current_instance() -> str:
    """Return the instance token this proxy process was started with, or ``""``."""
    return os.environ.get(INSTANCE_ENV, "")
