"""Read the forge and the pull request number from a CI environment.

Every function takes the environment as a mapping, never ``os.environ``
directly, so a test passes a plain ``dict`` and the CLI passes the
process environment. An empty value counts as unset: a CI variable
declared but left blank (``REPOACH_ADVISORY_POST: ""``) means the same
as an absent one.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

from ..project.config import ForgeName

PR_SOURCE_FLAG = "flag"
"""``pr_source`` when the number came from ``--pr``."""

PR_ENV_SOURCES: tuple[str, ...] = (
    "REPOACH_PR_NUMBER",
    "CI_MERGE_REQUEST_IID",
    "GITHUB_EVENT_PATH",
    "GITHUB_REF",
)
"""Variables read for the pull request number, in precedence order."""

_PULL_REF = re.compile(r"^refs/pull/(\d+)/(?:merge|head)$")


class NoPullRequestError(Exception):
    """No pull request number could be resolved from the flag or the environment.

    Attributes:
        looked_at: The variables consulted, in order.
        notes: Why a present variable did not yield a number.
    """

    def __init__(self, looked_at: tuple[str, ...], notes: tuple[str, ...] = ()) -> None:
        self.looked_at = looked_at
        self.notes = notes
        detail = f" ({'; '.join(notes)})" if notes else ""
        super().__init__(f"no pull request: none of {', '.join(looked_at)} is set{detail}")


def env_value(env: Mapping[str, str], name: str) -> str | None:
    """Return ``env[name]`` stripped, or None when absent or blank."""
    value = env.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def first_set(env: Mapping[str, str], names: tuple[str, ...]) -> str | None:
    """Return the first of ``names`` holding a non-blank value, or None."""
    for name in names:
        if env_value(env, name) is not None:
            return name
    return None


def _positive_int(raw: object) -> int | None:
    """Return ``raw`` as a positive int when it is one (int or digit string)."""
    if isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw if raw > 0 else None
    if isinstance(raw, str) and raw.strip().isdigit():
        number = int(raw.strip())
        return number if number > 0 else None
    return None


def _number_from_event(path: Path) -> tuple[int | None, str | None]:
    """Read the pull request number out of a GitHub Actions event payload.

    Reads ``pull_request.number``, then ``issue.number`` when the issue
    is a pull request (``issue.pull_request`` set), then
    ``inputs.pr_number`` (``workflow_dispatch``).

    Args:
        path: The ``GITHUB_EVENT_PATH`` file.

    Returns:
        ``(number, None)`` on success, ``(None, note)`` saying why not.
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"GITHUB_EVENT_PATH unreadable: {exc}"
    if not isinstance(payload, dict):
        return None, "GITHUB_EVENT_PATH is not a JSON object"
    pull = payload.get("pull_request")
    if isinstance(pull, dict) and (number := _positive_int(pull.get("number"))) is not None:
        return number, None
    issue = payload.get("issue")
    if isinstance(issue, dict):
        if issue.get("pull_request"):
            number = _positive_int(issue.get("number"))
            if number is not None:
                return number, None
        else:
            return None, "GITHUB_EVENT_PATH: the issue event is not a pull request"
    inputs = payload.get("inputs")
    if isinstance(inputs, dict) and (number := _positive_int(inputs.get("pr_number"))) is not None:
        return number, None
    return None, "GITHUB_EVENT_PATH: the event names no pull request"


def resolve_pr_number(explicit: int | None, env: Mapping[str, str]) -> tuple[int, str]:
    """Resolve the pull request number and say where it came from.

    Order: the explicit argument (``flag``), ``REPOACH_PR_NUMBER``,
    GitLab's ``CI_MERGE_REQUEST_IID``, GitHub's ``GITHUB_EVENT_PATH``
    payload, then ``GITHUB_REF`` of the form ``refs/pull/<n>/merge``.

    Args:
        explicit: The ``--pr`` value, None when absent.
        env: The environment.

    Returns:
        ``(number, source)``; ``source`` is ``flag`` or the variable name.

    Raises:
        NoPullRequestError: Nothing yields a positive number; names every
            variable consulted and why a present one did not count.
    """
    if explicit is not None:
        if explicit <= 0:
            raise NoPullRequestError((PR_SOURCE_FLAG,), (f"--pr {explicit} is not positive",))
        return explicit, PR_SOURCE_FLAG
    notes: list[str] = []
    for name in ("REPOACH_PR_NUMBER", "CI_MERGE_REQUEST_IID"):
        raw = env_value(env, name)
        if raw is None:
            continue
        number = _positive_int(raw)
        if number is not None:
            return number, name
        notes.append(f"{name}={raw!r} is not a positive integer")
    event_path = env_value(env, "GITHUB_EVENT_PATH")
    if event_path is not None:
        number, note = _number_from_event(Path(event_path))
        if number is not None:
            return number, "GITHUB_EVENT_PATH"
        if note is not None:
            notes.append(note)
    ref = env_value(env, "GITHUB_REF")
    if ref is not None:
        match = _PULL_REF.match(ref)
        if match is not None:
            return int(match.group(1)), "GITHUB_REF"
        notes.append(f"GITHUB_REF={ref!r} is not a pull request ref")
    raise NoPullRequestError(PR_ENV_SOURCES, tuple(notes))


def detect_ci_forge(env: Mapping[str, str]) -> tuple[ForgeName | None, str]:
    """Return the forge whose CI runs this process, and the variable that says so.

    Args:
        env: The environment.

    Returns:
        ``("gitlab", "GITLAB_CI")``, ``("github", "GITHUB_ACTIONS")``, or
        ``(None, "none")`` outside both.
    """
    if env_value(env, "GITLAB_CI") is not None or env_value(env, "CI_MERGE_REQUEST_IID"):
        return "gitlab", "GITLAB_CI" if env_value(env, "GITLAB_CI") else "CI_MERGE_REQUEST_IID"
    if env_value(env, "GITHUB_ACTIONS") is not None:
        return "github", "GITHUB_ACTIONS"
    return None, "none"
