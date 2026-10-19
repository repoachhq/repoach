#!/usr/bin/env python3
"""A disposable ``gh`` impersonator that answers repoach's read calls from GitLab.

SP-GH-GLAB-SHIM (worksite W1). Installed first on ``PATH`` under the name
``gh`` it lets ``repoach review pr <iid> --dry-run`` run unchanged against a
GitLab merge request, so the GitHub-to-any-forge gap can be measured before a
``Forge`` protocol is designed. It is deliberately not part of the package:
a shim must never be the thing that posts or merges.

Read calls translated (the ones ``review pr --dry-run`` makes, measured from
``review/gh_client.py`` and its callers):

* ``pr diff N`` -> ``glab api projects/:id/merge_requests/N/raw_diffs``
  (``glab mr diff`` drops the ``diff --git`` headers repoach parses).
* ``pr view N --json <fields>`` -> ``glab api projects/:id/merge_requests/N``
  with the field mapping in :data:`PR_VIEW_FIELD_MAP`.
* ``api [--paginate] [--slurp] repos/:owner/:repo/pulls/N/comments`` ->
  the MR's ``DiffNote`` notes in GitHub's inline-comment shape.
* ``api repos/:owner/:repo/pulls/N/reviews/<id>/comments`` -> ``[]``
  (GitLab has no review object).
* ``api [--paginate] [--slurp] repos/:owner/:repo/issues/N/comments`` ->
  the MR's non-system notes in GitHub's issue-comment shape.

Every write (any ``--method`` other than ``GET``, any ``-f``/``-F``/
``--input`` field, ``pr comment|review|merge|create|edit|close``) exits
:data:`EXIT_REFUSED_WRITE`; any other argv exits :data:`EXIT_UNSUPPORTED`.
When ``GH_GLAB_SHIM_LOG`` names a file, each argv and its outcome is appended
to it as one JSON line, which is how the W1 run lists the calls it made.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

EXIT_REFUSED_WRITE = 70
EXIT_UNSUPPORTED = 64

PR_VIEW_FIELD_MAP: dict[str, str] = {
    "number": "iid",
    "title": "title",
    "body": "description",
    "headRefName": "source_branch",
    "baseRefName": "target_branch",
    "headRefOid": "sha",
    "author": "author.username",
    "state": "state",
    "url": "web_url",
}

GITLAB_STATE_TO_GITHUB: dict[str, str] = {
    "opened": "OPEN",
    "merged": "MERGED",
    "closed": "CLOSED",
    "locked": "OPEN",
}

WRITE_PR_SUBCOMMANDS = frozenset(
    {"comment", "review", "merge", "create", "edit", "close", "reopen", "ready"}
)

_PULL_COMMENTS_RE = re.compile(r"^repos/[^/]+/[^/]+/pulls/(\d+)/comments$")
_REVIEW_COMMENTS_RE = re.compile(r"^repos/[^/]+/[^/]+/pulls/(\d+)/reviews/(\d+)/comments$")
_ISSUE_COMMENTS_RE = re.compile(r"^repos/[^/]+/[^/]+/issues/(\d+)/comments$")

GlabRunner = Callable[[list[str]], "GlabResult"]


@dataclass(frozen=True)
class GlabResult:
    """Outcome of one ``glab`` invocation.

    Attributes:
        returncode: Exit code of ``glab``.
        stdout: Captured stdout.
        stderr: Captured stderr.
    """

    returncode: int
    stdout: str
    stderr: str


@dataclass(frozen=True)
class ShimResult:
    """What the shim prints and returns for one ``gh`` argv.

    Attributes:
        returncode: Exit code the shim exits with.
        stdout: Text written to stdout.
        stderr: Text written to stderr.
        kind: Short label of the translation taken, for the call log.
    """

    returncode: int
    stdout: str
    stderr: str
    kind: str


def run_glab(args: list[str]) -> GlabResult:
    """Run the real ``glab`` with ``args`` in the current directory.

    The binary is ``GH_GLAB_SHIM_GLAB`` when set, else ``glab`` on ``PATH``.

    Args:
        args: Arguments after the ``glab`` binary.

    Returns:
        The captured :class:`GlabResult`; ``returncode`` 127 when no
        ``glab`` is reachable.
    """
    binary = os.environ.get("GH_GLAB_SHIM_GLAB") or shutil.which("glab")
    if binary is None:
        return GlabResult(returncode=127, stdout="", stderr="glab not installed")
    proc = subprocess.run([binary, *args], capture_output=True, text=True, check=False, timeout=120)
    return GlabResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _dig(payload: dict[str, object], dotted: str) -> object:
    """Return the value at a dotted path in a nested dict, or ``None``."""
    node: object = payload
    for key in dotted.split("."):
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node


def map_pr_view(mr: dict[str, object], fields: Sequence[str]) -> dict[str, object]:
    """Map a GitLab merge-request payload onto ``gh pr view --json`` fields.

    Args:
        mr: The JSON object returned by ``GET projects/:id/merge_requests/:iid``.
        fields: The GitHub field names requested after ``--json``.

    Returns:
        A dict holding exactly the requested fields; ``author`` becomes
        ``{"login": <username>}`` and ``state`` is upper-cased to GitHub's
        vocabulary. An unknown field maps to ``None``.
    """
    out: dict[str, object] = {}
    for field in fields:
        source = PR_VIEW_FIELD_MAP.get(field)
        value = _dig(mr, source) if source else None
        if field == "author":
            value = {"login": value} if isinstance(value, str) else None
        elif field == "state" and isinstance(value, str):
            value = GITLAB_STATE_TO_GITHUB.get(value, value.upper())
        elif field == "body" and value is None:
            value = ""
        out[field] = value
    return out


def map_diff_notes(discussions: list[object]) -> list[dict[str, object]]:
    """Flatten GitLab MR discussions into GitHub inline review comments.

    Only ``DiffNote`` notes are inline comments. The first note of a
    discussion is the thread root (``in_reply_to_id`` ``None``); later
    notes reply to it.

    Args:
        discussions: The JSON array from ``GET .../merge_requests/:iid/discussions``.

    Returns:
        GitHub-shaped dicts carrying ``id``, ``in_reply_to_id``, ``path``,
        ``line``, ``body`` and ``user.login``.
    """
    comments: list[dict[str, object]] = []
    for discussion in discussions:
        notes = discussion.get("notes") if isinstance(discussion, dict) else None
        if not isinstance(notes, list):
            continue
        diff_notes = [n for n in notes if isinstance(n, dict) and n.get("type") == "DiffNote"]
        root_id: object = None
        for note in diff_notes:
            position = note.get("position") if isinstance(note.get("position"), dict) else {}
            comments.append(
                {
                    "id": note.get("id"),
                    "in_reply_to_id": root_id,
                    "path": position.get("new_path") or position.get("old_path"),
                    "line": position.get("new_line") or position.get("old_line"),
                    "body": note.get("body") or "",
                    "user": {"login": _dig(note, "author.username")},
                }
            )
            if root_id is None:
                root_id = note.get("id")
    return comments


def map_issue_notes(notes: list[object]) -> list[dict[str, object]]:
    """Map GitLab MR notes onto GitHub issue comments, dropping system notes.

    Args:
        notes: The JSON array from ``GET .../merge_requests/:iid/notes``.

    Returns:
        GitHub-shaped dicts carrying ``id``, ``body`` and ``user.login``.
    """
    return [
        {
            "id": n.get("id"),
            "body": n.get("body") or "",
            "user": {"login": _dig(n, "author.username")},
        }
        for n in notes
        if isinstance(n, dict) and not n.get("system")
    ]


def _is_write_api_call(args: Sequence[str]) -> bool:
    """True when a ``gh api`` argv would send anything but a bare GET."""
    for i, arg in enumerate(args):
        if arg in {"-X", "--method"} and i + 1 < len(args) and args[i + 1].upper() != "GET":
            return True
        if arg.startswith("--method=") and arg.split("=", 1)[1].upper() != "GET":
            return True
        if arg in {"-f", "-F", "--field", "--raw-field", "--input"}:
            return True
    return False


def _glab_json(glab: GlabRunner, endpoint: str, *, paginate: bool) -> tuple[object, str | None]:
    """Fetch one GitLab endpoint as JSON; return ``(payload, error)``."""
    args = ["api", endpoint]
    if paginate:
        args.insert(1, "--paginate")
    res = glab(args)
    if res.returncode != 0:
        return None, res.stderr or f"glab exited {res.returncode}"
    text = res.stdout.strip()
    if not text:
        return [], None
    try:
        return _decode_concatenated(text), None
    except (json.JSONDecodeError, ValueError) as exc:
        return None, f"glab returned non-JSON: {exc}"


def _decode_concatenated(text: str) -> object:
    """Decode one JSON value, or several concatenated arrays merged into one.

    ``glab api --paginate`` prints each page as its own JSON array back to
    back, which :func:`json.loads` rejects as extra data.
    """
    decoder = json.JSONDecoder()
    values: list[object] = []
    pos = 0
    while pos < len(text):
        value, pos = decoder.raw_decode(text, pos)
        values.append(value)
        while pos < len(text) and text[pos].isspace():
            pos += 1
    if len(values) == 1:
        return values[0]
    merged: list[object] = []
    for value in values:
        if not isinstance(value, list):
            raise ValueError("expected every paginated page to be a JSON array")
        merged.extend(value)
    return merged


def _emit_list(items: list[dict[str, object]], *, slurp: bool, kind: str) -> ShimResult:
    """Render a list the way ``gh api`` would, slurped into pages or not."""
    payload: object = [items] if slurp else items
    return ShimResult(0, json.dumps(payload), "", kind)


def _handle_pr(args: list[str], glab: GlabRunner) -> ShimResult:
    """Translate ``gh pr <sub> ...``."""
    if not args:
        return ShimResult(EXIT_UNSUPPORTED, "", "gh-glab-shim: bare 'pr'\n", "unsupported")
    sub, rest = args[0], args[1:]
    if sub in WRITE_PR_SUBCOMMANDS:
        return ShimResult(
            EXIT_REFUSED_WRITE, "", f"gh-glab-shim: refused write 'pr {sub}'\n", "refused-write"
        )
    if sub == "diff" and rest and rest[0].isdigit():
        res = glab(["api", f"projects/:id/merge_requests/{rest[0]}/raw_diffs"])
        return ShimResult(res.returncode, res.stdout, res.stderr, "pr-diff")
    if sub == "view" and rest and rest[0].isdigit() and "--json" in rest:
        fields_arg = rest[rest.index("--json") + 1] if rest.index("--json") + 1 < len(rest) else ""
        fields = [f for f in fields_arg.split(",") if f]
        mr, error = _glab_json(glab, f"projects/:id/merge_requests/{rest[0]}", paginate=False)
        if error is not None or not isinstance(mr, dict):
            return ShimResult(1, "", f"gh-glab-shim: {error}\n", "pr-view")
        return ShimResult(0, json.dumps(map_pr_view(mr, fields)), "", "pr-view")
    return ShimResult(
        EXIT_UNSUPPORTED, "", f"gh-glab-shim: unsupported 'pr {sub}'\n", "unsupported"
    )


def _handle_api(args: list[str], glab: GlabRunner) -> ShimResult:
    """Translate ``gh api ...`` read calls."""
    if _is_write_api_call(args):
        return ShimResult(
            EXIT_REFUSED_WRITE, "", "gh-glab-shim: refused write 'api'\n", "refused-write"
        )
    slurp = "--slurp" in args
    paths = [a for a in args if not a.startswith("-")]
    endpoint = paths[0] if paths else ""
    if _REVIEW_COMMENTS_RE.match(endpoint):
        return _emit_list([], slurp=slurp, kind="review-id-comments")
    if match := _PULL_COMMENTS_RE.match(endpoint):
        data, error = _glab_json(
            glab, f"projects/:id/merge_requests/{match.group(1)}/discussions", paginate=True
        )
        if error is not None or not isinstance(data, list):
            return ShimResult(1, "", f"gh-glab-shim: {error}\n", "pull-comments")
        return _emit_list(map_diff_notes(data), slurp=slurp, kind="pull-comments")
    if match := _ISSUE_COMMENTS_RE.match(endpoint):
        data, error = _glab_json(
            glab, f"projects/:id/merge_requests/{match.group(1)}/notes", paginate=True
        )
        if error is not None or not isinstance(data, list):
            return ShimResult(1, "", f"gh-glab-shim: {error}\n", "issue-comments")
        return _emit_list(map_issue_notes(data), slurp=slurp, kind="issue-comments")
    return ShimResult(
        EXIT_UNSUPPORTED, "", f"gh-glab-shim: unsupported api '{endpoint}'\n", "unsupported"
    )


def dispatch(argv: list[str], glab: GlabRunner = run_glab) -> ShimResult:
    """Translate one ``gh`` argv (without the program name) into a result.

    Args:
        argv: Arguments as ``gh`` would receive them.
        glab: Runner for the real ``glab``; injected by tests.

    Returns:
        The :class:`ShimResult` to print and exit with.
    """
    if not argv:
        return ShimResult(EXIT_UNSUPPORTED, "", "gh-glab-shim: no command\n", "unsupported")
    head, rest = argv[0], argv[1:]
    if head == "pr":
        return _handle_pr(rest, glab)
    if head == "api":
        return _handle_api(rest, glab)
    return ShimResult(EXIT_UNSUPPORTED, "", f"gh-glab-shim: unsupported '{head}'\n", "unsupported")


def _append_call_log(argv: list[str], result: ShimResult) -> None:
    """Append one JSON line describing the call to ``GH_GLAB_SHIM_LOG``."""
    target = os.environ.get("GH_GLAB_SHIM_LOG")
    if not target:
        return
    line = json.dumps(
        {
            "argv": argv,
            "kind": result.kind,
            "returncode": result.returncode,
            "stdout_chars": len(result.stdout),
        }
    )
    with Path(target).open("a", encoding="utf-8") as fh:
        fh.write(line + "\n")


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch, log, print, return the exit code."""
    args = list(sys.argv[1:] if argv is None else argv)
    result = dispatch(args)
    _append_call_log(args, result)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
