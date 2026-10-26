"""Evidence the refuter serves its judge, filling the refuter's context budget.

Measured on Yoob MRs !324/!327 (2026-10-25): with a fixed 25-line window
around the cited range, the judge refuted 13 of 13 findings and an
independent re-read found 8 of those verdicts wrong, each one reasoning
"<X> is not in the evidence, so the claim cannot be confirmed". The
claims named code outside the window: a test that does not exist, a
second table elsewhere in the tree, the rest of the cited file.

Owner rules (2026-10-25, 2026-10-26): the refuter's context must be
large enough for it to do its job, and it is measured in tokens, never
in money. The evidence budget is therefore the refuter's own
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
   ``N:*``, at most :data:`GREP_MAX_HITS` hits in all, nearest first:
   the cited file, the files the claim names, tests, the rest), and
   whether each path the claim names is present;
3. the PR's diff hunks touching the cited file;
4. the spec section the finding cites (rows and paragraphs carrying a
   label such as ``G3`` or ``AC2``; the whole spec when the claim names
   the spec, or is a ``spec_gap``, without a label).

One ``git grep`` and at most one ``git ls-files`` run per finding, each
within :data:`GREP_TIMEOUT_S`, and the whole assembly within
:data:`ASSEMBLY_DEADLINE_S`. Truncation happens only when the budget,
the hit cap or the deadline is reached, and every item not
served is named in :attr:`EvidenceBundle.truncated`; the text then opens
with a ``PARTIAL EVIDENCE`` line so the judge answers ``cannot_judge``
for a claim resting on the missing part. Sizes use the one estimator
the spec names, :data:`TOKEN_ESTIMATOR` (``ceil(utf8_bytes / 3)``). No
cost figure (``max_cost_usd``, the usage ledger, ``refuter_tokens``)
reaches this module: cost is reported after the run, and never cuts or
shapes the evidence.

Every line of the bundle starts with a line number, ``---``, ``path``,
``search`` or a diff prefix (``+``, ``-``, space, ``@@``, ``diff``,
``index``), so PR-head content can never produce a column-one
``VERDICT:`` line (SP-REFUTER-INJECTION-HARDEN).
"""

from __future__ import annotations

import builtins
import keyword
import math
import re
import time
from collections.abc import Callable, Collection, Mapping, Sequence
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

REFUTER_RULE_VERSION = 2
"""Version of the rules that decide which words of a claim are identifiers and which veto.

Bumped whenever :data:`STOP_WORDS` or the partial-hit veto changes: a
finding left ``needs judgement`` under an earlier version is re-judged
at its head (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G3). Version 1 was
SP-REFUTER-JUDGES-WHAT-IT-CAN as first shipped."""

_PYTHON_WORDS = (
    frozenset(keyword.kwlist)
    | frozenset(keyword.softkwlist)
    | frozenset(name for name in dir(builtins) if not name.startswith("_"))
)
"""Python keywords, soft keywords and builtins (``None``, ``return``, ``Exception``...)."""

_COMMON_TOKENS = frozenset(
    {
        "self",
        "args",
        "kwargs",
        "true",
        "false",
        "null",
        "none",
        "nil",
        "undefined",
        "http",
        "https",
        "github",
        "gitlab",
        "localhost",
        "www",
        "html",
        "json",
        "yaml",
        "toml",
        "utf8",
        "ascii",
        "const",
        "function",
        "func",
        "void",
        "string",
        "bool",
        "boolean",
        "number",
        "public",
        "private",
        "protected",
        "static",
        "final",
        "async",
        "await",
        "yield",
        "this",
        "super",
        "throw",
        "throws",
        "catch",
        "else",
        "elif",
        "then",
        "done",
        "todo",
        "fixme",
        "info",
        "debug",
        "warn",
        "warning",
        "error",
        "errors",
    }
)
"""Tokens every language and log line carries; their hits say nothing about a claim."""

_ENGLISH_WORDS = frozenset(
    {
        "access",
        "after",
        "again",
        "also",
        "always",
        "before",
        "call",
        "calls",
        "case",
        "cases",
        "change",
        "check",
        "code",
        "could",
        "data",
        "does",
        "each",
        "every",
        "fails",
        "field",
        "file",
        "files",
        "from",
        "here",
        "into",
        "item",
        "items",
        "just",
        "line",
        "lines",
        "message",
        "messages",
        "must",
        "name",
        "names",
        "never",
        "only",
        "path",
        "paths",
        "result",
        "results",
        "should",
        "show",
        "some",
        "test",
        "tests",
        "text",
        "that",
        "there",
        "these",
        "they",
        "those",
        "token",
        "tokens",
        "used",
        "user",
        "users",
        "uses",
        "value",
        "values",
        "when",
        "where",
        "which",
        "while",
        "will",
        "with",
        "without",
        "would",
    }
)
"""Generic English words a claim's backticked spans or URLs split into."""

STOP_WORDS = _PYTHON_WORDS | _COMMON_TOKENS | _ENGLISH_WORDS
"""Words never searched as a claim's identifiers, so never a veto (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G1).

Measured on PR #260 (2026-10-26): the veto deferred two correct
refutations on ``except``, ``Exception``, ``return``, ``None``,
``message``, ``https``, ``access`` and ``token`` — words any fix of the
claim must contain, so the finding could never be settled. Python names
match exactly; the common tokens and English words match case-insensitively."""


def is_stop_word(token: str) -> bool:
    """Return True when ``token`` is a keyword, builtin, common token or generic English word."""
    return token in _PYTHON_WORDS or token.lower() in _COMMON_TOKENS | _ENGLISH_WORDS


GREP_TIMEOUT_S = 60
"""Seconds the one ``git grep`` (and the one ``git ls-files``) of a finding may run.

Sized for the review runner, a loaded machine: 30 s deferred every
finding there."""

ASSEMBLY_DEADLINE_S = 180
"""Wall-clock seconds the evidence of one finding may take; what is left is named, not served."""


class GrepTimedOutError(RuntimeError):
    """The head-tree ``git grep`` ran out of time; the timeout in seconds is its ``seconds``."""

    def __init__(self, seconds: int) -> None:
        super().__init__(f"git grep timed out after {seconds} s")
        self.seconds = seconds


_TEST_PATH = re.compile(
    r"(?:^|/)(?:tests?|__tests__|spec)/|(?:^|/)test_[^/]*$|_test\.\w+$|\.(?:test|spec)\.\w+$"
)
"""A test file: ranked right after the files the claim names."""

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
        unserved: Identifiers of the claim of which nothing was served
            although the tree holds them, or whose search could not run.
        partial: Identifiers of the claim whose cut hits may hold what the
            claim rests on: some hits inside the cited file were cut
            (``partial_in_cited``), or the cited file holds none and some
            hits elsewhere were cut. A refutation naming one is deferred.
        partial_in_cited: The identifiers of ``partial`` with a hit
            inside the cited file left unserved.
        partial_elsewhere: Identifiers every hit of which inside the
            cited file was served, with hits elsewhere cut; recorded, never
            a veto (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G2).
        needed_tokens: Evidence tokens the cited file, the hits of the
            claim's identifiers and the diff hunks take in full (what was
            served plus :attr:`shortfall_tokens`).
        shortfall_tokens: Tokens of that core evidence the ceiling left
            unserved; 0 when the cited file, the hits and the hunks were
            all served (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G5).
        timed_out: Why the search did not run when load is the cause
            (``grep timed out after 60 s``, ``assembly deadline of 180 s
            reached``), else empty: tells a load-caused deferral from an
            identifier the budget left out.
    """

    text: str
    reads: tuple[str, ...]
    truncated: tuple[str, ...] = ()
    tokens: int = 0
    budget_tokens: int = 0
    unserved: tuple[str, ...] = ()
    partial: tuple[str, ...] = ()
    timed_out: str = ""
    partial_in_cited: tuple[str, ...] = ()
    partial_elsewhere: tuple[str, ...] = ()
    needed_tokens: int = 0
    shortfall_tokens: int = 0


@dataclass
class _Assembly:
    """The evidence under construction against a token budget."""

    budget: int
    deadline: float = math.inf
    deadline_s: float = ASSEMBLY_DEADLINE_S
    timed_out: str = ""
    sections: list[str] = field(default_factory=list)
    reads: list[str] = field(default_factory=list)
    truncated: list[str] = field(default_factory=list)
    unserved: list[str] = field(default_factory=list)
    partial: list[str] = field(default_factory=list)
    partial_in_cited: list[str] = field(default_factory=list)
    partial_elsewhere: list[str] = field(default_factory=list)
    used: int = 0
    shortfall: int = 0

    def remaining(self) -> float:
        """Return the wall-clock seconds left before :data:`ASSEMBLY_DEADLINE_S`."""
        return self.deadline - time.monotonic()

    def out_of_time(self, step: str) -> bool:
        """Return True, naming ``step`` as not served, once the deadline has passed."""
        if self.remaining() > 0:
            return False
        self.truncated.append(f"{step}: {self.deadline_s:g} s assembly deadline reached")
        return True

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
    assembly.shortfall += sum(widths[:start]) + sum(widths[end:])
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
    words, never shorter than four characters and never a
    :data:`STOP_WORDS` entry; paths from file-name-shaped tokens (a bare file name is
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
            and not is_stop_word(token)
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


def tracked_files(repo_root: Path, *, timeout_s: float) -> tuple[str, ...] | None:
    """Return the tracked files of the tree at ``repo_root``; ``None`` when ``git`` could not say.

    One ``git ls-files`` per finding, shared by every bare file name the
    claim names.
    """
    result = spawn(["git", "ls-files", "-z"], cwd=repo_root, timeout_s=max(1, math.ceil(timeout_s)))
    if not result.ok:
        _log.info("refuter.ls_files_unavailable", returncode=result.returncode)
        return None
    return tuple(p for p in result.stdout.split("\0") if p)


def resolve_claim_path(repo_root: Path, rel: str, tracked: Sequence[str]) -> tuple[str, ...]:
    """Return the files a path the claim names designates, ``()`` when absent.

    A path with a directory is checked as given (path-jailed); a bare
    file name (``test_x.py``) is looked up in ``tracked``, so "the test
    file test_x.py" meets wherever it lives.
    """
    if "/" in rel:
        return (rel,) if _inside(repo_root, rel) else ()
    return tuple(p for p in tracked if Path(p).name == rel and _inside(repo_root, p))


def _word(identifier: str) -> re.Pattern[str]:
    """Return the whole-word pattern ``git grep -w`` matches ``identifier`` with."""
    return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(identifier)}(?![A-Za-z0-9_])")


def grep_head_tree(
    repo_root: Path, identifiers: Sequence[str], *, timeout_s: float = GREP_TIMEOUT_S
) -> dict[str, list[tuple[str, int]]] | None:
    """Return, per identifier, every ``(path, line)`` where it occurs as a word.

    One ``git grep -n -I -F -w`` over the tracked files of the tree at
    ``repo_root`` (local, read-only) searches every identifier at once.
    Returns ``None`` when the search could not run (not a git work tree,
    git missing); an empty list is a measured absence.

    Raises:
        GrepTimedOutError: The search ran past ``timeout_s``.
    """
    wanted = [i for i in identifiers if _IDENTIFIER.match(i)]
    if not wanted:
        return {}
    argv = ["git", "grep", "--no-color", "--null", "-n", "-I", "-F", "-w"]
    for identifier in wanted:
        argv += ["-e", identifier]
    seconds = max(1, math.ceil(timeout_s))
    result = spawn([*argv, "--"], cwd=repo_root, timeout_s=seconds)
    if result.returncode == -1:
        _log.info("refuter.grep_timed_out", identifiers=wanted, timeout_s=seconds)
        raise GrepTimedOutError(seconds)
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


def rank_hits(
    hits: Sequence[tuple[str, int]], *, cited_path: str, named_files: Collection[str]
) -> list[tuple[str, int]]:
    """Order hits by proximity to the finding: cited file, files the claim names, tests, rest.

    The order is stable inside each rank, so ``git grep``'s path order
    is kept.
    """

    def _rank(hit: tuple[str, int]) -> int:
        path = hit[0]
        if path == cited_path:
            return 0
        if path in named_files:
            return 1
        if _TEST_PATH.search(path):
            return 2
        return 3

    return sorted(hits, key=_rank)


def _allocate(wanted: Mapping[str, int], cap: int) -> dict[str, int]:
    """Split ``cap`` hits between identifiers: an equal share each, the rest to who wants more."""
    share = {name: min(n, cap // max(1, len(wanted))) for name, n in wanted.items()}
    left = cap - sum(share.values())
    for name, n in wanted.items():
        extra = min(n - share[name], left)
        share[name] += extra
        left -= extra
    return share


def _hit_blocks(hits: Sequence[tuple[str, int]]) -> list[tuple[str, int, int, tuple[int, ...]]]:
    """Group hits into ``(path, first, last, hit lines)`` blocks of ±:data:`GREP_CONTEXT_LINES`.

    Files keep the order of their first hit in ``hits`` (the proximity rank).
    """
    by_path: dict[str, list[int]] = {}
    for path, line in hits:
        by_path.setdefault(path, []).append(line)
    blocks: list[tuple[str, int, int, tuple[int, ...]]] = []
    for path, lines in by_path.items():
        for line in sorted(lines):
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
) -> list[tuple[str, int]]:
    """Serve ``allowed`` hits of ``identifier`` with context; return the hits served."""
    served: list[tuple[str, int]] = []
    for path, first, last, marks in _hit_blocks(allowed):
        if assembly.out_of_time(f"search {identifier}: hits in {_one_line(path)}"):
            continue
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
            served.extend((path, mark) for mark in marks)
            continue
        used_before = assembly.used
        for mark in marks:
            hit_line = f"{mark}:* {file_lines[mark - 1]}" if mark <= len(file_lines) else ""
            single = f"--- {_one_line(path)}: hit line only (budget) ---\n{hit_line}"
            if hit_line and assembly.fits(single):
                assembly.add(single)
                served.append((path, mark))
                assembly.truncated.append(
                    f"search {identifier}: context of {_one_line(path)}:{mark} (budget)"
                )
            else:
                assembly.truncated.append(
                    f"search {identifier}: hit {_one_line(path)}:{mark} (budget)"
                )
        assembly.shortfall += max(0, estimate_tokens(block) + 1 - (assembly.used - used_before))
    return served


def _serve_named_file(assembly: _Assembly, lines_of: _Lines, path: str) -> None:
    """Serve a file the claim names, whole, when it fits; name it otherwise."""
    if assembly.out_of_time(f"file {_one_line(path)}"):
        return
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


def _named_paths(
    assembly: _Assembly, repo_root: Path, paths: tuple[str, ...], cited_path: str
) -> tuple[list[str], list[str]]:
    """Report whether each path the claim names is present; return ``(header lines, files)``."""
    lines: list[str] = []
    files: list[str] = []
    tracked: tuple[str, ...] | None = ()
    if any("/" not in rel for rel in paths):
        tracked = (
            None
            if assembly.out_of_time("bare file names of the claim")
            else tracked_files(repo_root, timeout_s=min(GREP_TIMEOUT_S, assembly.remaining()))
        )
    for rel in paths:
        if "/" not in rel and tracked is None:
            lines.append(f"path {_one_line(rel)}: could not check")
            assembly.reads.append(f"path {_one_line(rel)}: could not check")
            assembly.truncated.append(f"path {_one_line(rel)}: git ls-files could not run")
            continue
        found = resolve_claim_path(repo_root, rel, tracked or ())
        state = "present" if found else "absent"
        where = (
            f" ({', '.join(_one_line(p) for p in found[:5])})" if found and found[0] != rel else ""
        )
        lines.append(f"path {_one_line(rel)}: {state} in the head tree{where}")
        assembly.reads.append(f"path {_one_line(rel)}: {state}")
        files.extend(p for p in found[:2] if p != cited_path and p not in files)
    return lines, files


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

    Path presence first; then, per identifier, its hits with context,
    ranked by :func:`rank_hits` and capped at :data:`GREP_MAX_HITS` in
    all; last, the files the claim names, whole when they fit. Each
    identifier ends in :attr:`_Assembly.unserved` (hits exist, none
    served, or the search could not run), :attr:`_Assembly.partial`
    (a hit inside the cited file cut, or the cited file holds none and a
    hit elsewhere cut), :attr:`_Assembly.partial_elsewhere` (every hit
    inside the cited file served, some elsewhere cut) or none of them
    (all served, or a measured absence).
    """
    if not identifiers and not paths:
        return
    header = [
        f"--- head-tree search: git grep -w over tracked files, "
        f"±{GREP_CONTEXT_LINES} lines per hit ---"
    ]
    path_lines, named_files = _named_paths(assembly, repo_root, paths, cited_path)
    assembly.add("\n".join([*header, *path_lines]))
    lines_of = _Lines(reader)
    found_hits: dict[str, list[tuple[str, int]]] | None = {}
    if identifiers:
        if assembly.out_of_time("head-tree search"):
            assembly.timed_out = f"assembly deadline of {assembly.deadline_s:g} s reached"
            found_hits = None
        else:
            try:
                found_hits = grep_head_tree(
                    repo_root, identifiers, timeout_s=min(GREP_TIMEOUT_S, assembly.remaining())
                )
            except GrepTimedOutError as exc:
                _log.warning(
                    "refuter.search_timed_out",
                    identifiers=list(identifiers),
                    timeout_s=exc.seconds,
                    consequence="claims naming these identifiers are deferred, not judged",
                )
                assembly.timed_out = f"grep timed out after {exc.seconds} s"
                found_hits = None
    if found_hits is None:
        for identifier in identifiers:
            assembly.add(
                f"search `{identifier}`: could not run", f"grep {identifier}: could not run"
            )
            assembly.unserved.append(identifier)
        assembly.truncated.append("search: git grep could not run")
        found_hits = {}

    def _outside(hits: list[tuple[str, int]]) -> list[tuple[str, int]]:
        return [
            h
            for h in hits
            if not (h[0] == cited_path and cited_served[0] < h[1] <= cited_served[1])
        ]

    outside = {name: _outside(hits) for name, hits in found_hits.items()}
    shares = _allocate({name: len(hits) for name, hits in outside.items()}, GREP_MAX_HITS)
    for identifier, hits in found_hits.items():
        others = outside[identifier]
        own = len(hits) - len(others)
        allowed = rank_hits(others, cited_path=cited_path, named_files=named_files)[
            : shares[identifier]
        ]
        if len(others) > len(allowed):
            assembly.truncated.append(
                f"search {identifier}: {len(others) - len(allowed)} hits over the "
                f"{GREP_MAX_HITS}-hit cap"
            )
        assembly.add(
            f"search `{identifier}`: {len(hits)} hits, {own} inside the cited file above, "
            f"{len(allowed)} below (nearest first)"
        )
        served = _serve_hits(assembly, lines_of, identifier, allowed)
        assembly.reads.append(f"grep {identifier}: {len(hits)} hits ({len(served)} served)")
        _classify(assembly, identifier, hits=hits, own=own, served=served, cited_path=cited_path)
    for path in named_files:
        _serve_named_file(assembly, lines_of, path)


def _classify(
    assembly: _Assembly,
    identifier: str,
    *,
    hits: Sequence[tuple[str, int]],
    own: int,
    served: Sequence[tuple[str, int]],
    cited_path: str,
) -> None:
    """File ``identifier`` as unserved, partial, partly served elsewhere, or whole.

    The claim is about the cited file (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS
    G2): an identifier whose every hit inside it was served never vetoes,
    whatever was cut elsewhere.
    """
    if not hits or len(served) + own == len(hits):
        return
    if not served and not own:
        assembly.unserved.append(identifier)
        return
    in_cited = sum(1 for path, _ in hits if path == cited_path)
    served_in_cited = own + sum(1 for path, _ in served if path == cited_path)
    if served_in_cited < in_cited:
        assembly.partial.append(identifier)
        assembly.partial_in_cited.append(identifier)
    elif in_cited:
        assembly.partial_elsewhere.append(identifier)
    else:
        assembly.partial.append(identifier)


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
            assembly.shortfall += estimate_tokens(block) + 1
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
    deadline_s: float = ASSEMBLY_DEADLINE_S,
    ceiling_tokens: int | None = None,
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
            ``context_tokens`` less the prompt and the reply reserve); with
            ``ceiling_tokens``, the floor the budget grows from.
        diff: The PR's unified diff; its hunks on ``path`` are served.
        specs: ``(spec id, markdown)`` candidates for the cited section.
        claim_is_spec_gap: Serve a whole spec when the claim cites no label.
        deadline_s: Wall-clock seconds the assembly may take
            (:data:`ASSEMBLY_DEADLINE_S`); steps past it are named in
            ``truncated``.
        ceiling_tokens: When given, the budget adapts to the evidence
            (SP-REFUTER-VETO-NAMES-REAL-IDENTIFIERS G5): the cited file,
            the identifiers' hits and the diff hunks are served up to this
            many tokens, and the final budget is ``max(budget_tokens,
            what they took)``, which the spec section fills. What the
            ceiling left out is :attr:`EvidenceBundle.shortfall_tokens`.

    Returns:
        The bundle, or ``None`` when the file holds none of the cited lines.
    """
    core_budget = budget_tokens if ceiling_tokens is None else max(ceiling_tokens, budget_tokens)
    assembly = _Assembly(
        budget=core_budget, deadline=time.monotonic() + deadline_s, deadline_s=deadline_s
    )
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
    if diff and not assembly.out_of_time(f"diff of {_one_line(path)}"):
        _diff(assembly, diff, path)
    needed = assembly.used + assembly.shortfall
    if ceiling_tokens is not None:
        assembly.budget = max(budget_tokens, assembly.used)
    if specs and not assembly.out_of_time("spec section"):
        _specs(assembly, specs, claim=claim, claim_is_spec_gap=claim_is_spec_gap)
    sections = list(assembly.sections)
    if assembly.truncated:
        sections.insert(
            0,
            f"--- PARTIAL EVIDENCE: the {assembly.budget}-token budget, the hit cap or the deadline left "
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
        budget_tokens=assembly.budget,
        unserved=tuple(assembly.unserved),
        partial=tuple(assembly.partial),
        timed_out=assembly.timed_out,
        partial_in_cited=tuple(assembly.partial_in_cited),
        partial_elsewhere=tuple(assembly.partial_elsewhere),
        needed_tokens=needed,
        shortfall_tokens=assembly.shortfall,
    )
