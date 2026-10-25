"""Evidence the refuter serves its judge, filling the refuter's context budget.

Measured on Yoob MRs !324/!327 (2026-10-25): with a fixed 25-line window
around the cited range, the judge refuted 13 of 13 findings and an
independent re-read found 8 of those verdicts wrong, each one reasoning
"<X> is not in the evidence, so the claim cannot be confirmed". The
claims named code outside the window: a test that does not exist, a
second table elsewhere in the tree, the rest of the cited file.

Owner rule (2026-10-25): the refuter's context must be large enough for
it to do its job. The evidence budget is therefore the refuter's own
``context_tokens`` (:data:`~repoach.project.config.REFUTER_CONTEXT_TOKENS_FLOOR`
as the floor, ``[gates].refuter_context_tokens``), and
:func:`build_evidence` fills it in this priority, all read-only from the
pinned head tree and path-jailed under ``repo_root``:

1. the whole cited file, numbered, the cited lines marked ``N:>`` — a
   file over the budget is cut to a contiguous slice around the cited
   range, never smaller than :data:`EVIDENCE_CONTEXT_LINES` on each side;
2. for every identifier the claim names (backticked spans, CamelCase,
   snake_case, UPPER_SNAKE, the cited module when the claim is about
   tests), the ``git grep -n -I -F -w`` hits over the tracked files with
   :data:`GREP_CONTEXT_LINES` lines on each side (hit lines marked
   ``N:*``, at most :data:`GREP_MAX_HITS` hits in all), and whether each
   path the claim names is present;
3. the PR's diff hunks touching the cited file;
4. the spec section the finding cites (rows and paragraphs carrying a
   label such as ``G3`` or ``AC2``; the whole spec when the claim names
   the spec, or is a ``spec_gap``, without a label).

Truncation happens only when the budget is exhausted, and every item not
served is named in :attr:`EvidenceBundle.truncated`; the text then opens
with a ``PARTIAL EVIDENCE`` line so the judge answers ``cannot_judge``
for a claim resting on the missing part. Sizes use the disclosed
estimator of SP-AGENT-CONTEXT-LOOP, ``ceil(utf8_bytes / 3)``.

Every line of the bundle starts with a line number, ``---``, ``path``,
``search`` or a diff prefix (``+``, ``-``, space, ``@@``, ``diff``,
``index``), so PR-head content can never produce a column-one
``VERDICT:`` line (SP-REFUTER-INJECTION-HARDEN).
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger
from ..forge.runner import spawn

_log = get_logger(__name__)

TOKEN_ESTIMATOR = "ceil(utf8_bytes / 3)"
"""The disclosed size estimator (SP-AGENT-CONTEXT-LOOP ``token_estimator``)."""

EVIDENCE_CONTEXT_LINES = 25
"""Lines on each side of the cited range always served: the floor, never the default."""

GREP_CONTEXT_LINES = 10
"""Lines served on each side of a head-tree search hit."""

GREP_MAX_HITS = 50
"""Head-tree search hits served at most, across every identifier of a claim."""

_MAX_IDENTIFIERS = 6
"""Identifiers of one claim searched at most, in order of appearance."""

_MIN_IDENTIFIER_LEN = 4
"""Shorter tokens match too widely to say anything about a claim."""

_GREP_TIMEOUT_S = 120
"""Seconds the one ``git grep`` of a finding may run (a cold worktree on a loaded disk)."""

GREP_COMMON_HITS = 200
"""An identifier with more hits than this is too common to say anything specific."""

_PATH_EXTENSIONS = (
    "py|pyi|ts|tsx|js|jsx|sh|bash|yml|yaml|toml|ini|cfg|json|md|rst|txt|"
    "v|sv|vhd|vhdl|c|h|cc|cpp|hpp|rs|go|java|kt|sql|mako|html|css"
)

_PATH_TOKEN = re.compile(
    rf"(?<![\w/.-])((?:[\w.-]+/)*[\w-][\w.-]*\.(?:{_PATH_EXTENSIONS}))(?![\w])"
)
"""A relative file path or file name the claim names."""

_BACKTICKED = re.compile(r"`([^`\n]{3,120})`")
"""A backticked span of the claim."""

_BARE_IDENTIFIERS = re.compile(
    r"\b("
    r"[A-Z][a-z0-9]+(?:[A-Z][a-z0-9]*)+"
    r"|[A-Za-z][a-z0-9]*(?:_[A-Za-z0-9]+)+"
    r"|_[A-Za-z0-9]+(?:_[A-Za-z0-9]+)*"
    r"|[A-Z][A-Z0-9]+(?:_[A-Z0-9]+)+"
    r")\b"
)
"""CamelCase, snake_case, _private and UPPER_SNAKE identifiers."""

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
"""One identifier, the only token shape handed to ``git grep``."""

_TEST_WORDS = re.compile(r"\btest", re.IGNORECASE)
"""A claim about tests: the cited file's module name is searched too."""

_GENERIC_STEMS = frozenset({"__init__", "index", "main", "conftest", "setup", "utils"})
"""Module names whose search says nothing about the cited file."""

_SPEC_ID = re.compile(r"\bSP-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")
"""A spec id the claim names."""

_SPEC_LABEL = re.compile(r"\b(?:G|NG|AC|F|D|Q|A|C|M)\d{1,3}\b")
"""A spec section label the claim cites (goal, criterion, fact, decision...)."""

FileReader = Callable[[str], str | None]
"""Reads a repo-relative path from the head tree, ``None`` when absent or outside it."""


def estimate_tokens(text: str) -> int:
    """Return the size of ``text`` under :data:`TOKEN_ESTIMATOR`."""
    return math.ceil(len(text.encode("utf-8")) / 3)


@dataclass(frozen=True)
class EvidenceBundle:
    """The evidence text served to the judge, what it holds and what it lacks.

    Attributes:
        text: The evidence, in priority order.
        reads: One entry per source served, such as
            ``src/m.py:1-120 (whole file)``, ``grep AcceleratorBoard: 9
            hits (9 served)``, ``path tests/test_x.py: absent``,
            ``diff src/m.py: 2 hunks`` or ``spec SP-X: AC2``.
        truncated: One entry per item not served because the budget ran
            out or a hit cap was reached; empty when the evidence is whole.
        tokens: Size of ``text`` under :data:`TOKEN_ESTIMATOR`.
        budget_tokens: The budget the evidence was assembled against.
    """

    text: str
    reads: tuple[str, ...]
    truncated: tuple[str, ...] = ()
    tokens: int = 0
    budget_tokens: int = 0


@dataclass
class _Assembly:
    """The evidence under construction against a token budget."""

    budget: int
    sections: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    used: int = 0

    def fits(self, text: str) -> bool:
        """Return True when ``text`` still fits in the remaining budget."""
        return self.used + estimate_tokens(text) + 1 <= self.budget

    def add(self, text: str, read: str | None = None) -> None:
        """Append ``text`` (and its read label), charging the budget."""
        self.sections.append(text)
        self.used += estimate_tokens(text) + 1
        if read is not None:
            self.reads.append(read)


def _one_line(text: str) -> str:
    """Collapse newlines so a PR-supplied string stays on one evidence line."""
    return text.replace("\r", " ").replace("\n", " ")


def _numbered(
    lines: Sequence[str], start: int, end: int, marked: Callable[[int], str]
) -> list[str]:
    """Return lines ``[start, end)`` (0-based) as ``N<mark> text``, 1-based numbers."""
    return [f"{n}{marked(n)} {lines[n - 1]}" for n in range(start + 1, end + 1)]


def _slice_bounds(widths: Sequence[int], first: int, last: int, budget: int) -> tuple[int, int]:
    """Return the 0-based ``[start, end)`` served around cited lines ``first..last``.

    The floor window (:data:`EVIDENCE_CONTEXT_LINES` each side) always;
    then one line at a time on alternating sides while ``budget`` allows.
    """
    total = len(widths)
    start = max(0, first - 1 - EVIDENCE_CONTEXT_LINES)
    end = min(total, last + EVIDENCE_CONTEXT_LINES)
    used = sum(widths[start:end])
    grew = True
    while grew:
        grew = False
        if start > 0 and used + widths[start - 1] <= budget:
            start -= 1
            used += widths[start]
            grew = True
        if end < total and used + widths[end] <= budget:
            used += widths[end]
            end += 1
            grew = True
    return start, end


def _cited_file(
    assembly: _Assembly, source: str, *, path: str, line_start: int, line_end: int
) -> tuple[int, int] | None:
    """Serve the cited file (priority 1); return the 0-based slice served, ``None`` if absent."""
    lines = source.splitlines()
    if not lines or line_start > len(lines):
        return None
    last = max(line_start, min(line_end, len(lines)))
    widths = [estimate_tokens(f"{n + 1}:> {line}") + 1 for n, line in enumerate(lines)]
    head_room = assembly.budget - assembly.used - 64
    if sum(widths) <= head_room:
        start, end = 0, len(lines)
    else:
        start, end = _slice_bounds(widths, line_start, last, head_room)
    whole = start == 0 and end == len(lines)
    shown = "whole file" if whole else f"of {len(lines)} lines"
    header = (
        f"--- {_one_line(path)}: lines {start + 1}-{end} ({shown}); "
        f"cited lines {line_start}-{last} are marked ':>' ---"
    )
    body = _numbered(lines, start, end, lambda n: ":>" if line_start <= n <= last else ":")
    assembly.add("\n".join([header, *body]), f"{_one_line(path)}:{start + 1}-{end} ({shown})")
    if start > 0:
        assembly.truncated.append(f"{_one_line(path)}: lines 1-{start} (budget)")
    if end < len(lines):
        assembly.truncated.append(f"{_one_line(path)}: lines {end + 1}-{len(lines)} (budget)")
    return start, end


def _is_version(token: str) -> bool:
    """Return True for a dotted number such as ``1.2.md`` misread as a file name."""
    return bool(re.fullmatch(r"[\d.]+\.\w+", token))


def claim_identifiers(claim: str, *, cited_file: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(identifiers, paths)`` the claim names, each deduplicated, in order.

    Identifiers come from backticked spans (``Foo.bar()`` yields ``Foo``
    and ``bar``) and from bare CamelCase / snake_case / UPPER_SNAKE
    words; paths from file-name-shaped tokens (a bare file name is
    looked up among the tracked files, :func:`resolve_claim_path`). When
    the claim talks about tests, the cited file's module name is
    searched first, so "no test covers this module" meets the tests that
    import it.
    """
    identifiers: list[str] = []
    paths: list[str] = []

    def _add(token: str) -> None:
        if (
            len(token) >= _MIN_IDENTIFIER_LEN
            and _IDENTIFIER.match(token)
            and not _SPEC_LABEL.fullmatch(token)
            and token not in identifiers
        ):
            identifiers.append(token)

    stem = Path(cited_file).stem
    if _TEST_WORDS.search(claim) and stem not in _GENERIC_STEMS:
        _add(stem)
    for match in _PATH_TOKEN.finditer(claim):
        candidate = match.group(1)
        if candidate not in paths and candidate != cited_file and not _is_version(candidate):
            paths.append(candidate)
    for span in _BACKTICKED.findall(claim):
        if _PATH_TOKEN.fullmatch(span.strip()):
            continue
        for part in re.split(r"[^A-Za-z0-9_]+", span):
            _add(part)
    without_paths = _SPEC_ID.sub(" ", _PATH_TOKEN.sub(" ", claim))
    for word in _BARE_IDENTIFIERS.findall(without_paths):
        _add(word)
    return tuple(identifiers[:_MAX_IDENTIFIERS]), tuple(paths[:_MAX_IDENTIFIERS])


def _inside(repo_root: Path, rel: str) -> bool:
    """Return True when ``rel`` is a file inside ``repo_root``."""
    root = repo_root.resolve()
    try:
        target = (root / rel).resolve()
    except (OSError, RuntimeError) as exc:
        _log.debug("refuter.claim_path_unresolvable", path=rel, error=type(exc).__name__)
        return False
    return target.is_relative_to(root) and target.is_file()


def resolve_claim_path(repo_root: Path, rel: str) -> tuple[str, ...]:
    """Return the tracked files a path the claim names designates, ``()`` when absent.

    A path with a directory is checked as given (path-jailed); a bare
    file name (``test_x.py``) is looked up among the tracked files with
    ``git ls-files``, so "the test file test_x.py" meets wherever it lives.
    """
    if "/" in rel:
        return (rel,) if _inside(repo_root, rel) else ()
    result = spawn(
        ["git", "ls-files", "-z", "--", rel, f"*/{rel}"], cwd=repo_root, timeout_s=_GREP_TIMEOUT_S
    )
    if not result.ok:
        _log.info("refuter.ls_files_unavailable", path=rel, returncode=result.returncode)
        return ()
    found = [p for p in result.stdout.split("\0") if p and Path(p).name == rel]
    return tuple(p for p in found if _inside(repo_root, p))


def _word(identifier: str) -> re.Pattern[str]:
    """Return the whole-word pattern ``git grep -w`` matches ``identifier`` with."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])")


def grep_head_tree(
    repo_root: Path, identifiers: Sequence[str]
) -> dict[str, list[tuple[str, int]]] | None:
    """Return, per identifier, every ``(path, line)`` where it occurs as a word.

    One ``git grep -n -I -F -w`` over the tracked files of the tree at
    ``repo_root`` (local, read-only) searches every identifier at once,
    so a slow disk costs one timeout, not one per identifier. Returns
    ``None`` when the search could not run (not a git work tree, git
    missing, timeout); an empty list is a measured absence.
    """
    wanted = [i for i in identifiers if _IDENTIFIER.match(i)]
    if not wanted:
        return {}
    argv = ["git", "grep", "--no-color", "--null", "-n", "-I", "-F", "-w"]
    for identifier in wanted:
        argv += ["-e", identifier]
    result = spawn([*argv, "--"], cwd=repo_root, timeout_s=_GREP_TIMEOUT_S)
    if result.returncode == 1 and not result.stdout:
        return {identifier: [] for identifier in wanted}
    if not result.ok:
        _log.info(
            "refuter.grep_unavailable",
            identifiers=wanted,
            returncode=result.returncode,
            stderr=result.stderr[:160],
        )
        return None
    patterns = {identifier: _word(identifier) for identifier in wanted}
    hits: dict[str, list[tuple[str, int]]] = {identifier: [] for identifier in wanted}
    for row in result.stdout.splitlines():
        parts = row.split("\0", 2)
        if len(parts) != 3 or not parts[1].isdigit():
            continue
        for identifier, pattern in patterns.items():
            if pattern.search(parts[2]):
                hits[identifier].append((parts[0], int(parts[1])))
    return hits


def _hit_blocks(hits: Sequence[tuple[str, int]]) -> list[tuple[str, int, int, tuple[int, ...]]]:
    """Group hits into ``(path, first, last, hit lines)`` blocks of ±:data:`GREP_CONTEXT_LINES`."""
    blocks: list[tuple[str, int, int, tuple[int, ...]]] = []
    for path, line in hits:
        first, last = max(1, line - GREP_CONTEXT_LINES), line + GREP_CONTEXT_LINES
        if blocks and blocks[-1][0] == path and first <= blocks[-1][2] + 1:
            prev = blocks[-1]
            blocks[-1] = (path, prev[1], max(prev[2], last), (*prev[3], line))
        else:
            blocks.append((path, first, last, (line,)))
    return blocks


class _Lines:
    """Path-jailed, cached line access to head-tree files."""

    def __init__(self, reader: FileReader) -> None:
        self._reader = reader
        self._cache: dict[str, list[str] | None] = {}

    def __call__(self, path: str) -> list[str] | None:
        """Return the lines of ``path``, ``None`` when unreadable."""
        if path not in self._cache:
            text = self._reader(path)
            self._cache[path] = None if text is None else text.splitlines()
        return self._cache[path]


def _serve_hits(
    assembly: _Assembly, lines_of: _Lines, identifier: str, allowed: Sequence[tuple[str, int]]
) -> int:
    """Serve ``allowed`` hits of ``identifier`` with context; return the hits served."""
    served = 0
    for path, first, last, marks in _hit_blocks(allowed):
        file_lines = lines_of(path)
        if file_lines is None:
            assembly.truncated.append(f"search {identifier}: {_one_line(path)} unreadable")
            continue
        end = min(last, len(file_lines))
        block = "\n".join(
            [
                f"--- {_one_line(path)}: lines {first}-{end} (hit lines marked ':*') ---",
                *_numbered(file_lines, first - 1, end, lambda n, m=marks: ":*" if n in m else ":"),
            ]
        )
        if assembly.fits(block):
            assembly.add(block)
            served += len(marks)
            continue
        for mark in marks:
            hit_line = f"{mark}:* {file_lines[mark - 1]}" if mark <= len(file_lines) else ""
            single = f"--- {_one_line(path)}: hit line only (budget) ---\n{hit_line}"
            if hit_line and assembly.fits(single):
                assembly.add(single)
                served += 1
                assembly.truncated.append(
                    f"search {identifier}: context of {_one_line(path)}:{mark} (budget)"
                )
            else:
                assembly.truncated.append(
                    f"search {identifier}: hit {_one_line(path)}:{mark} (budget)"
                )
    return served


def _serve_named_file(assembly: _Assembly, lines_of: _Lines, path: str) -> None:
    """Serve a file the claim names, whole, when it fits; name it otherwise."""
    file_lines = lines_of(path)
    if file_lines is None:
        assembly.truncated.append(f"file {_one_line(path)}: unreadable")
        return
    block = "\n".join(
        [
            f"--- {_one_line(path)}: lines 1-{len(file_lines)} (named by the claim, whole file) ---",
            *_numbered(file_lines, 0, len(file_lines), lambda _n: ":"),
        ]
    )
    if assembly.fits(block):
        assembly.add(block, f"file {_one_line(path)}: whole ({len(file_lines)} lines)")
    else:
        assembly.truncated.append(f"file {_one_line(path)}: {len(file_lines)} lines (budget)")


def _search(
    assembly: _Assembly,
    reader: FileReader,
    repo_root: Path,
    *,
    identifiers: tuple[str, ...],
    paths: tuple[str, ...],
    cited_path: str,
    cited_served: tuple[int, int],
) -> None:
    """Serve the head-tree search (priority 2).

    Path presence first; then, per identifier, its hits with context.
    An identifier with more than :data:`GREP_COMMON_HITS` hits says
    nothing specific: its count is given and its hits are named as not
    served, leaving the :data:`GREP_MAX_HITS` cap to the specific ones.
    Last, the files the claim names, whole when they fit.
    """
    if not identifiers and not paths:
        return
    header = [
        f"--- head-tree search: git grep -w over tracked files, "
        f"±{GREP_CONTEXT_LINES} lines per hit ---"
    ]
    named_files: list[str] = []
    for rel in paths:
        found = resolve_claim_path(repo_root, rel)
        state = "present" if found else "absent"
        where = (
            f" ({', '.join(_one_line(p) for p in found[:5])})" if found and found[0] != rel else ""
        )
        header.append(f"path {_one_line(rel)}: {state} in the head tree{where}")
        assembly.reads.append(f"path {_one_line(rel)}: {state}")
        named_files.extend(p for p in found[:2] if p != cited_path and p not in named_files)
    assembly.add("\n".join(header))
    lines_of = _Lines(reader)
    found_hits = grep_head_tree(repo_root, identifiers) if identifiers else {}
    if found_hits is None:
        for identifier in identifiers:
            assembly.add(
                f"search `{identifier}`: could not run", f"grep {identifier}: could not run"
            )
        assembly.truncated.append("search: git grep could not run")
        found_hits = {}

    def _outside(hits: list[tuple[str, int]]) -> list[tuple[str, int]]:
        return [
            h
            for h in hits
            if not (h[0] == cited_path and cited_served[0] < h[1] <= cited_served[1])
        ]

    specific = [i for i in found_hits if len(_outside(found_hits[i])) <= GREP_COMMON_HITS]
    per_identifier = max(1, GREP_MAX_HITS // max(1, len(specific)))
    remaining_hits = GREP_MAX_HITS
    for identifier, hits in found_hits.items():
        others = _outside(hits)
        own = len(hits) - len(others)
        allowed = others[: min(per_identifier, remaining_hits)] if identifier in specific else []
        remaining_hits -= len(allowed)
        if identifier not in specific:
            assembly.truncated.append(
                f"search {identifier}: {len(others)} hits, too common to serve "
                f"(over {GREP_COMMON_HITS})"
            )
        elif len(others) > len(allowed):
            assembly.truncated.append(
                f"search {identifier}: {len(others) - len(allowed)} hits over the "
                f"{GREP_MAX_HITS}-hit cap"
            )
        assembly.add(
            f"search `{identifier}`: {len(hits)} hits, {own} inside the cited file above, "
            f"{len(allowed)} below"
        )
        served = _serve_hits(assembly, lines_of, identifier, allowed)
        assembly.reads.append(f"grep {identifier}: {len(hits)} hits ({served} served)")
    for path in named_files:
        _serve_named_file(assembly, lines_of, path)


def _diff_hunks(diff: str, path: str) -> list[str]:
    """Return the hunks of ``diff`` touching ``path``, each with its ``@@`` header."""
    hunks: list[list[str]] = []
    current: list[str] | None = None
    in_file = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_file = line.endswith(f" b/{path}")
            current = None
        elif in_file and line.startswith("@@"):
            current = [line]
            hunks.append(current)
        elif in_file and current is not None and line[:1] in ("+", "-", " ", "\\", ""):
            current.append(line)
    return ["\n".join(hunk) for hunk in hunks]


def _diff(assembly: _Assembly, diff: str, path: str) -> None:
    """Serve the PR's diff hunks touching the cited file (priority 3)."""
    hunks = _diff_hunks(diff, path) if diff else []
    if not hunks:
        return
    served = 0
    for index, hunk in enumerate(hunks, start=1):
        block = f"--- diff of {_one_line(path)}: hunk {index} of {len(hunks)} ---\n{hunk}"
        if assembly.fits(block):
            assembly.add(block)
            served += 1
        else:
            assembly.truncated.append(
                f"diff {_one_line(path)}: hunk {index} of {len(hunks)} (budget)"
            )
    assembly.reads.append(f"diff {_one_line(path)}: {served} of {len(hunks)} hunks")


_BLOCK_OPENERS = re.compile(r"^(?:\||[-*] |#|\d+\. )")
"""A Markdown line that opens its own block: table row, list item, heading."""


def _spec_blocks(markdown: str, labels: Sequence[str]) -> list[tuple[int, int]]:
    """Return 0-based ``[start, end)`` blocks (paragraphs, list items, table rows) citing ``labels``."""
    lines = markdown.splitlines()
    blocks: list[tuple[int, int]] = []
    start: int | None = None
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or _BLOCK_OPENERS.match(stripped):
            if start is not None:
                blocks.append((start, index))
            start = index if stripped else None
        elif start is None:
            start = index
    if start is not None:
        blocks.append((start, len(lines)))
    pattern = re.compile(r"\b(?:" + "|".join(re.escape(label) for label in labels) + r")\b")
    return [(a, b) for a, b in blocks if pattern.search("\n".join(lines[a:b]))]


def _specs(
    assembly: _Assembly, specs: Sequence[tuple[str, str]], *, claim: str, claim_is_spec_gap: bool
) -> None:
    """Serve the spec section the finding cites (priority 4)."""
    labels = list(dict.fromkeys(_SPEC_LABEL.findall(claim)))
    named = set(_SPEC_ID.findall(claim))
    for spec_id, markdown in specs:
        lines = markdown.splitlines()
        if labels:
            ranges = _spec_blocks(markdown, labels)
            scope = ", ".join(labels)
        elif spec_id in named or claim_is_spec_gap:
            ranges = [(0, len(lines))]
            scope = "whole spec"
        else:
            continue
        if not ranges:
            continue
        served = 0
        for start, end in ranges:
            block = "\n".join(
                [
                    f"--- spec {_one_line(spec_id)}: lines {start + 1}-{end} ---",
                    *_numbered(lines, start, end, lambda _n: ":"),
                ]
            )
            if assembly.fits(block):
                assembly.add(block)
                served += 1
            else:
                assembly.truncated.append(
                    f"spec {_one_line(spec_id)}: lines {start + 1}-{end} (budget)"
                )
        assembly.reads.append(
            f"spec {_one_line(spec_id)}: {scope} ({served} of {len(ranges)} blocks)"
        )


def build_evidence(
    source: str,
    *,
    repo_root: Path,
    reader: FileReader,
    path: str,
    line_start: int,
    line_end: int,
    claim: str,
    budget_tokens: int,
    diff: str = "",
    specs: Sequence[tuple[str, str]] = (),
    claim_is_spec_gap: bool = False,
) -> EvidenceBundle | None:
    """Assemble the judge's evidence for one finding within ``budget_tokens``.

    Args:
        source: The cited file's text at the head.
        repo_root: The pinned head tree.
        reader: Path-jailed reader of the head tree (search hit context).
        path: The cited file, repo-relative.
        line_start: First cited line.
        line_end: Last cited line.
        claim: The finding's claim, searched for identifiers, paths,
            spec ids and spec labels.
        budget_tokens: Tokens the evidence may take (the refuter's
            ``context_tokens`` less the prompt and the reply reserve).
        diff: The PR's unified diff; its hunks on ``path`` are served.
        specs: ``(spec id, markdown)`` candidates for the cited section.
        claim_is_spec_gap: Serve a whole spec when the claim cites no label.

    Returns:
        The bundle, or ``None`` when the file holds none of the cited lines.
    """
    assembly = _Assembly(budget=budget_tokens)
    served = _cited_file(assembly, source, path=path, line_start=line_start, line_end=line_end)
    if served is None:
        return None
    identifiers, paths = claim_identifiers(claim, cited_file=path)
    _search(
        assembly,
        reader,
        repo_root,
        identifiers=identifiers,
        paths=paths,
        cited_path=path,
        cited_served=served,
    )
    _diff(assembly, diff, path)
    _specs(assembly, specs, claim=claim, claim_is_spec_gap=claim_is_spec_gap)
    sections = list(assembly.sections)
    if assembly.truncated:
        sections.insert(
            0,
            f"--- PARTIAL EVIDENCE: the {budget_tokens}-token budget or a hit cap left "
            f"{len(assembly.truncated)} item(s) unserved, listed at the end ---",
        )
        sections.append(
            "--- not served ---\n"
            + "\n".join(f"--- {_one_line(item)}" for item in assembly.truncated)
        )
    text = "\n".join(sections)
    return EvidenceBundle(
        text=text,
        reads=tuple(assembly.reads),
        truncated=tuple(assembly.truncated),
        tokens=estimate_tokens(text),
        budget_tokens=budget_tokens,
    )
