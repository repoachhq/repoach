#!/usr/bin/env python3
"""PostToolUse hook: warn on comment-policy violations in edited files.

Repoach adaptation of the sharp-agent original, two layers:

1. The repo's own SP-NO-INLINE-COMMENTS-GATE scanner
   (repoach.lint.no_inline_comments) runs on edited ``.py`` files under
   src/, tests/, scripts/ — the same scanner pre-commit and CI run, so
   inline-comment and ``noqa`` violations surface at edit time instead
   of commit time.
2. The generic useless-comment patterns from the original hook
   (tracking codes, banner separators, bare TODOs, dated comments)
   for every scanned file.

Detects but does NOT modify files. Warnings go to stderr, always
exits 0 — the blocking enforcement stays in the git/CI gates.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

TRACKING_CODE_RE = re.compile(
    r"#\s*(?:Sprint\s+\d+|US\s+\d|CR-\d|Week\s+\d{2})",
    re.IGNORECASE,
)
TRACKING_CODE_TS_RE = re.compile(
    r"//\s*(?:Sprint\s+\d+|US\s+\d|CR-\d|Week\s+\d{2})",
    re.IGNORECASE,
)

BANNER_DASHES_RE = re.compile(r"^#\s*[-─═]{40,}\s*$")
BANNER_UNICODE_RE = re.compile(r"^#\s*[─═]{2,}[^─═\n]{2,}[─═]{5,}\s*$")
BANNER_TS_DASHES_RE = re.compile(r"^//\s*[-─═]{40,}\s*$")
BANNER_TS_UNICODE_RE = re.compile(r"^//\s*[─═]{2,}[^─═\n]{2,}[─═]{5,}\s*$")

TODO_BARE_RE = re.compile(r"#\s*TODO\b(?!\s*\()", re.IGNORECASE)
TODO_BARE_TS_RE = re.compile(r"//\s*TODO\b(?!\s*\()", re.IGNORECASE)

DATED_RE = re.compile(r"#\s*(?:Added|Fixed?|Changed?)\s+\d{4}-\d{2}", re.IGNORECASE)
DATED_TS_RE = re.compile(r"//\s*(?:Added|Fixed?|Changed?)\s+\d{4}-\d{2}", re.IGNORECASE)

PRAGMA_PY_RE = re.compile(
    r"#\s*(?:type:\s*ignore|pylint:|fmt:|nosec|noinspection|isort:|ruff:|encoding|!)"
)
PRAGMA_TS_RE = re.compile(
    r"//\s*(?:eslint-disable|@ts-(?:ignore|expect-error|nocheck)|prettier-ignore)"
)

GOLDEN_RULE_ROOTS = {"src", "tests", "scripts"}


def check_repoach_gate(path: Path) -> list[tuple[int, str, str]]:
    """Run the repo's no-inline-comments scanner on one edited file.

    Returns an empty list outside the golden-rule roots or when the
    scanner cannot be imported (hook must never crash a session).
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "")
    if not project_dir:
        return []
    root = Path(project_dir)
    try:
        rel = path.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return []
    if not rel.parts or rel.parts[0] not in GOLDEN_RULE_ROOTS:
        return []
    src_dir = str(root / "src")
    sys.path.insert(0, src_dir)
    try:
        from repoach.lint.no_inline_comments import scan_file
    except ImportError:
        return []
    finally:
        if src_dir in sys.path:
            sys.path.remove(src_dir)
    return [(v.line, f"golden rule [{v.kind}]", v.body[:80]) for v in scan_file(path)]


def check_python(lines: list[str]) -> list[tuple[int, str, str]]:
    """Flag useless standalone ``#`` comment patterns in Python source."""
    issues: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("#"):
            continue
        if PRAGMA_PY_RE.search(stripped):
            continue

        if TRACKING_CODE_RE.search(stripped):
            issues.append((i, "tracking code in source", stripped[:80]))
        elif BANNER_DASHES_RE.match(stripped) or BANNER_UNICODE_RE.match(stripped):
            issues.append((i, "banner section separator", stripped[:80]))
        elif TODO_BARE_RE.search(stripped):
            issues.append((i, "bare TODO without owner/date", stripped[:80]))
        elif DATED_RE.search(stripped):
            issues.append((i, "dated comment (not a WHY)", stripped[:80]))
    return issues


def check_typescript(lines: list[str]) -> list[tuple[int, str, str]]:
    """Flag useless standalone ``//`` comment patterns in TS/JS source."""
    issues: list[tuple[int, str, str]] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if not stripped.startswith("//"):
            continue
        if PRAGMA_TS_RE.search(stripped):
            continue

        if TRACKING_CODE_TS_RE.search(stripped):
            issues.append((i, "tracking code in source", stripped[:80]))
        elif BANNER_TS_DASHES_RE.match(stripped) or BANNER_TS_UNICODE_RE.match(stripped):
            issues.append((i, "banner section separator", stripped[:80]))
        elif TODO_BARE_TS_RE.search(stripped):
            issues.append((i, "bare TODO without owner/date", stripped[:80]))
        elif DATED_TS_RE.search(stripped):
            issues.append((i, "dated comment (not a WHY)", stripped[:80]))
    return issues


def main() -> None:
    """Read the PostToolUse payload and report comment issues on stderr."""
    try:
        hook_input = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        return

    file_path = hook_input.get("tool_input", {}).get("file_path", "")
    if not file_path:
        return

    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix not in {".py", ".ts", ".tsx", ".js", ".jsx"}:
        return

    skip_parts = {"__pycache__", "node_modules", ".venv", "dist", "external", "alembic"}
    if any(part in path.parts for part in skip_parts):
        return

    try:
        source = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return

    lines = source.splitlines()
    if suffix == ".py":
        issues = check_repoach_gate(path) + check_python(lines)
    else:
        issues = check_typescript(lines)

    if not issues:
        return

    print(f"\n[comment-lint] {path.name} — {len(issues)} comment issue(s):", file=sys.stderr)
    for lineno, reason, snippet in issues:
        print(f"  line {lineno:4d}  [{reason}]  {snippet!r}", file=sys.stderr)
    print(
        "  Golden rule (SP-NO-INLINE-COMMENTS-GATE): zero inline comments,"
        " zero noqa — docstrings carry the why. Fix now; the pre-commit/CI"
        " gate will block otherwise.",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
