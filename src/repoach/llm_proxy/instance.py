"""Per-job proxy identity: a free port and an instance token (SP-JOB-PROXY-IS-PRIVATE-TO-ITS-JOB).

Two review jobs on two self-hosted runners of the same host used to start
their proxy on the same hardcoded port. The second bind failed, and the
second job's ``/health`` poll was answered by the first job's proxy, which
then refused every call with ``401 Invalid API key``. This module gives a
job its own port and a token the proxy echoes in ``/health``, so the job
can prove the proxy that answers is the one it started.

Command line (used by ``scripts/job_proxy.sh``)::

    python -m repoach.llm_proxy.instance port [--host H]
    python -m repoach.llm_proxy.instance check --url URL --expect TOKEN
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import socket
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Sequence
from enum import Enum
from typing import Any
from urllib.parse import urlsplit

_log = logging.getLogger(__name__)

INSTANCE_ENV = "REPOACH_PROXY_INSTANCE"
"""Env var carrying the token the job generated for its own proxy."""

EXIT_OWN = 0
EXIT_UNREACHABLE = 1
EXIT_USAGE = 2
EXIT_FOREIGN = 3


class HealthMatch(Enum):
    """What a ``/health`` answer says about who is listening."""

    OWN = "own"
    FOREIGN = "foreign"
    UNREACHABLE = "unreachable"


def current_instance() -> str:
    """Return the instance token this proxy process was started with, or ``""``."""
    return os.environ.get(INSTANCE_ENV, "")


def pick_free_port(host: str = "127.0.0.1") -> int:
    """Ask the kernel for a port that is free on ``host`` right now.

    The socket is closed before the proxy binds, so another process could
    take the port in between; the instance check catches that case.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def classify_health(body: Any, expected: str) -> HealthMatch:
    """Classify a decoded ``/health`` body against the expected instance token.

    Args:
        body: The decoded JSON body, or ``None`` when nothing answered.
        expected: The token the job passed to its proxy. Must be non-empty.

    Returns:
        ``OWN`` when the body is healthy and carries exactly ``expected``;
        ``UNREACHABLE`` when nothing (or nothing healthy) answered;
        ``FOREIGN`` when a healthy proxy answered with another token or none.
    """
    if not expected:
        raise ValueError("expected instance token must be non-empty")
    if not isinstance(body, dict) or body.get("status") != "healthy":
        return HealthMatch.UNREACHABLE
    if body.get("instance") == expected:
        return HealthMatch.OWN
    return HealthMatch.FOREIGN


def fetch_health(url: str, timeout_s: float = 1.0) -> Any:
    """Return the decoded JSON of ``GET url``, or ``None`` when it does not answer."""
    try:
        with urllib.request.urlopen(url, timeout=timeout_s) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError) as exc:
        _log.debug("proxy_health_unreachable url=%s error=%s", url, exc)
        return None


def check_health(
    url: str,
    expected: str,
    fetch: Callable[[str], Any] = fetch_health,
) -> tuple[HealthMatch, str]:
    """Probe ``url`` once and return the match plus a one-line explanation."""
    body = fetch(url)
    match = classify_health(body, expected)
    where = urlsplit(url).netloc or url
    if match is HealthMatch.OWN:
        return match, f"own proxy on {where}"
    if match is HealthMatch.FOREIGN:
        seen = body.get("instance") if isinstance(body, dict) else None
        return match, f"foreign proxy on {where} (instance={seen or 'none'})"
    return match, f"no healthy proxy on {where}"


_EXIT_BY_MATCH = {
    HealthMatch.OWN: EXIT_OWN,
    HealthMatch.FOREIGN: EXIT_FOREIGN,
    HealthMatch.UNREACHABLE: EXIT_UNREACHABLE,
}


def main(argv: Sequence[str] | None = None) -> int:
    """Run the ``port`` or ``check`` subcommand and return its exit code."""
    parser = argparse.ArgumentParser(prog="python -m repoach.llm_proxy.instance")
    sub = parser.add_subparsers(dest="cmd", required=True)
    port_p = sub.add_parser("port", help="print a free TCP port")
    port_p.add_argument("--host", default="127.0.0.1")
    check_p = sub.add_parser("check", help="probe /health and compare the instance token")
    check_p.add_argument("--url", required=True)
    check_p.add_argument("--expect", required=True)
    args = parser.parse_args(argv)

    if args.cmd == "port":
        print(pick_free_port(args.host))
        return EXIT_OWN
    if not args.expect:
        print("--expect must be non-empty", file=sys.stderr)
        return EXIT_USAGE
    match, message = check_health(args.url, args.expect, fetch=fetch_health)
    print(message)
    return _EXIT_BY_MATCH[match]


if __name__ == "__main__":
    sys.exit(main())
