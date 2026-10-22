"""Print a clean squash body from ``gh pr view --json commits`` on stdin.

``scripts/safe_merge.sh`` pipes the PR's commits through this module and
hands the result to ``gh pr merge --squash --body-file -``, so the
operator's merge path drops attribution exactly like
``GitHubForge.merge`` does.

Usage::

    gh pr view <N> --json commits | python -m repoach.conventions.squash_body
"""

from __future__ import annotations

import json
import sys

from repoach.conventions.attribution import squash_body


def commit_messages(payload: object) -> list[tuple[str, str]]:
    """Extract ``(headline, body)`` pairs from a ``gh pr view --json commits`` payload.

    Raises:
        ValueError: The payload is not an object with a ``commits`` list.
    """
    commits = payload.get("commits") if isinstance(payload, dict) else None
    if not isinstance(commits, list):
        raise ValueError("expected a JSON object with a 'commits' list")
    return [
        (str(entry.get("messageHeadline") or ""), str(entry.get("messageBody") or ""))
        for entry in commits
        if isinstance(entry, dict)
    ]


def main() -> int:
    """Read the payload on stdin and write the squash body on stdout."""
    try:
        messages = commit_messages(json.loads(sys.stdin.read()))
    except ValueError as exc:
        print(f"squash_body: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(squash_body(messages))
    return 0


if __name__ == "__main__":
    sys.exit(main())
