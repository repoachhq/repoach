"""``.githooks/commit-msg`` entry point: strip attribution, then judge the header.

The order matters: a message that is valid once its attribution trailer
is gone must not be refused before the strip. Stripping is silent apart
from one stderr line, so an agent's commit still lands — clean.

Usage::

    python -m repoach.conventions.commit_msg <message-file>

Exit codes: ``0`` the (possibly rewritten) message is accepted; ``1``
its header breaks a rule, one line per rule on stderr; ``2`` usage.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path

from repoach.conventions.attribution import strip_attribution
from repoach.conventions.header import header_violations, message_header

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2


def main(argv: Sequence[str] | None = None) -> int:
    """Rewrite the message file without attribution and judge its header."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -m repoach.conventions.commit_msg <message-file>", file=sys.stderr)
        return EXIT_USAGE
    path = Path(args[0])
    original = path.read_text(encoding="utf-8")
    cleaned = strip_attribution(original)
    if cleaned != original:
        path.write_text(cleaned, encoding="utf-8")
        print("commit-msg: stripped AI attribution from the commit message.", file=sys.stderr)
    violations = header_violations(cleaned)
    if not violations:
        return EXIT_OK
    print("commit-msg: commit message refused", file=sys.stderr)
    for rule in violations:
        print(f"  - {rule}", file=sys.stderr)
    print(f"  got: {message_header(cleaned)!r}", file=sys.stderr)
    print("  expected: type(scope): subject, at most 72 characters", file=sys.stderr)
    return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
