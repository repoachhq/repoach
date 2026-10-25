"""Mechanical finding verifiers (SP-VERIFIER-LIB, review redesign slice 4).

Transitions ``proposed`` findings to ``verified`` (the claim holds — a
real finding) or ``refuted`` (the repo disproves it — a hallucination),
reusing the hallucination guard's on-disk primitives. "Verified" means
the test really is missing / the docstring really is absent / the lint
really fires; "refuted" means the symbol, docstring or clean file
exists. The judged claim types (``design`` / ``security``) and the
executable ones (``broken_behavior`` / ``spec_gap``) are left
``proposed`` for slices 5-6. Dual-run: statuses are recorded, no merge
decision changes here.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..core.logging import get_logger
from ..gates.config import GatesConfig, applicable, load_gates_config
from ..lint.no_inline_comments import scan_file as scan_inline
from ..lint.no_silent_except import scan_file as scan_silent
from .findings import (
    ClaimType,
    Finding,
    FindingStatus,
    fetch_findings,
    record_verification_attempt,
    update_finding_status,
)
from .hallucination_guard import (
    _extract_missing_test_symbols,
    make_repo_file_reader,
)

_log = get_logger(__name__)

_LANGUAGE_METHOD = "language"
"""Verification method recorded when a Python-only verifier does not apply."""

_DEFERRED_RESULT = "not mechanically verifiable — slices 5-6"

_TEST_DIR_NAMES = frozenset({"tests", "test"})
"""Directory names whose ``.py`` files are test files wherever they sit."""

_WALK_SKIPPED_DIRS = frozenset({".git", "node_modules", ".venv", "venv", "dist", "build"})
"""Directories never walked when the root is not a git work tree."""


@dataclass(frozen=True)
class TestSearch:
    """Where a ``missing_test`` verifier looked for the covering tests.

    Attributes:
        found: ``(target, "path:line")`` for each target defined in a test file.
        searched: Number of Python test files read.
        at: Short sha of the tree searched, empty when not a git work tree.
    """

    __test__ = False

    found: tuple[tuple[str, str], ...]
    searched: int
    at: str

    def scope(self) -> str:
        """Name what was searched, ``searched <n> test files[ at <sha7>]``."""
        where = f" at {self.at}" if self.at else ""
        return f"searched {self.searched} test files{where}"


def is_python_test_file(path: str, test_roots: tuple[str, ...] = ()) -> bool:
    """Whether ``path`` is a Python test file.

    A ``.py`` file is a test file under a configured test root, under any
    ``tests/`` or ``test/`` directory (``api/tests/`` in a monorepo whose
    configured root is ``tests``), or named ``test_*.py`` / ``*_test.py``
    / ``conftest.py``. Code under review elsewhere never is
    (SP-MISSING-TEST-VERIFIER-SCOPE).
    """
    pure = PurePosixPath(path)
    if pure.suffix != ".py":
        return False
    name = pure.name
    if name.startswith("test_") or name.endswith("_test.py") or name == "conftest.py":
        return True
    if _TEST_DIR_NAMES.intersection(pure.parts[:-1]):
        return True
    return any(
        root and (path == root or path.startswith(root.rstrip("/") + "/")) for root in test_roots
    )


def _git_out(repo_root: Path, *args: str) -> str | None:
    """Return the stdout of a read-only ``git`` command, ``None`` when it fails."""
    try:
        proc = subprocess.run(
            ["git", *args], cwd=repo_root, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        _log.debug("finding_verifiers.git_unavailable", error=type(exc).__name__)
        return None
    return proc.stdout if proc.returncode == 0 else None


def _head_of(repo_root: Path) -> str | None:
    """Return the full HEAD sha when ``repo_root`` is the top of a git work tree."""
    top = _git_out(repo_root, "rev-parse", "--show-toplevel", "HEAD")
    if top is None:
        return None
    lines = top.split()
    if len(lines) != 2 or Path(lines[0]).resolve() != repo_root.resolve():
        return None
    return lines[1]


def _tracked_files(repo_root: Path) -> list[str] | None:
    """Return the tracked paths of the git work tree ``repo_root``."""
    listing = _git_out(repo_root, "ls-files", "-z")
    if listing is None:
        return None
    return [path for path in listing.split("\0") if path]


def _walked_files(repo_root: Path) -> list[str]:
    """Return every file under ``repo_root``, skipping vendored and build directories."""
    paths: list[str] = []
    for directory, subdirs, files in os.walk(repo_root):
        subdirs[:] = [d for d in subdirs if d not in _WALK_SKIPPED_DIRS]
        for name in files:
            paths.append(PurePosixPath(Path(directory, name).relative_to(repo_root)).as_posix())
    return paths


_DEFINITION = re.compile(r"^[ \t]*(?:async\s+)?(?:def|class)\s+([A-Za-z_]\w*)", re.MULTILINE)
"""A ``def`` / ``class`` line of a Python test file and the name it defines."""


@dataclass(frozen=True)
class _TestIndex:
    """Every name defined in the Python test files of one tree."""

    where: dict[str, str]
    searched: int
    at: str


_INDEX_CACHE: dict[tuple[str, str, tuple[str, ...]], _TestIndex] = {}
"""Test-name indexes of git trees, keyed by ``(root, head sha, test roots)``."""

_INDEX_CACHE_SIZE = 8
"""Trees kept in :data:`_INDEX_CACHE`; the oldest is dropped past it."""


def _build_index(
    repo_root: Path, paths: list[str], test_roots: tuple[str, ...], at: str
) -> _TestIndex:
    """Read every Python test file among ``paths`` once and index its definitions."""
    candidates = sorted(p for p in paths if is_python_test_file(p, test_roots))
    where: dict[str, str] = {}
    for path in candidates:
        try:
            text = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            _log.debug("finding_verifiers.test_file_skipped", path=path, error=type(exc).__name__)
            continue
        for match in _DEFINITION.finditer(text):
            name = match.group(1)
            if name not in where:
                line = text.count("\n", 0, match.start()) + 1
                where[name] = f"{path}:{line}"
    return _TestIndex(where=where, searched=len(candidates), at=at)


def _test_index(repo_root: Path, test_roots: tuple[str, ...]) -> _TestIndex:
    """Return the test-name index of ``repo_root``, built once per git head.

    A git work tree is listed and read once per ``(root, head sha, test
    roots)``, so a gate re-checking N findings at one head walks the
    tree once; a tree outside git is read on every call.
    """
    head = _head_of(repo_root)
    if head is None:
        return _build_index(repo_root, _walked_files(repo_root), test_roots, "")
    key = (str(repo_root.resolve()), head, test_roots)
    cached = _INDEX_CACHE.get(key)
    if cached is not None:
        return cached
    tracked = _tracked_files(repo_root)
    if tracked is None:
        return _build_index(repo_root, _walked_files(repo_root), test_roots, "")
    index = _build_index(repo_root, tracked, test_roots, head[:7])
    if len(_INDEX_CACHE) >= _INDEX_CACHE_SIZE:
        _INDEX_CACHE.pop(next(iter(_INDEX_CACHE)))
    _INDEX_CACHE[key] = index
    return index


def find_tests_by_name(
    repo_root: Path, targets: list[str], test_roots: tuple[str, ...] = ()
) -> TestSearch:
    """Look for ``def`` / ``class`` of each target in every Python test file of the tree.

    The tree is the tracked files of ``repo_root`` when it is a git work
    tree (the pinned head worktree), else every file under it. Only test
    files (:func:`is_python_test_file`) are read, so a test that lives
    in ``api/tests/`` is found while a symbol of the code under review
    never refutes the claim. A git tree is indexed once per head.

    Args:
        repo_root: Root of the tree to search.
        targets: Test names to look for (``test_``-prefixed).
        test_roots: The project's configured test roots.

    Returns:
        The :class:`TestSearch`; ``found`` keeps the order of ``targets``.
    """
    index = _test_index(repo_root, test_roots)
    found = tuple((t, index.where[t]) for t in targets if t in index.where)
    return TestSearch(found=found, searched=index.searched, at=index.at)


def _missing_test_search_targets(symbols: list[str]) -> list[str]:
    """Map extracted missing-test symbols to ``tests/``-searchable targets.

    A bare underscore-prefixed symbol (e.g. ``_parse_verdict``) names the
    code under review, not the test that would cover it, so it is
    translated to its ``test_``-prefixed counterpart before the
    tests-only searcher runs — the search target is always the covering
    test's expected name, never the src symbol itself
    (SP-MISSING-TEST-VERIFIER-SCOPE G2).

    Args:
        symbols: Ordered, de-duplicated symbols from
            :func:`_extract_missing_test_symbols`.

    Returns:
        Ordered, de-duplicated ``test_``-prefixed search targets.
    """
    targets: list[str] = []
    for symbol in symbols:
        target = symbol if symbol.startswith("test_") else f"test_{symbol.lstrip('_')}"
        if target not in targets:
            targets.append(target)
    return targets


def _verify_missing_test(
    finding: Finding, repo_root: Path, config: GatesConfig
) -> tuple[FindingStatus, str, str]:
    """Refute a missing-test claim only when a covering test exists in a test file.

    The covering test is looked for by name across every Python test
    file of the tree (:func:`find_tests_by_name`), not only under the
    top-level ``tests/`` nor in the file the finding cited: the existence
    of the code-under-test must never refute a "no test for X" claim
    (SP-MISSING-TEST-VERIFIER-SCOPE), and a test written in
    ``api/tests/`` must (SP-GATE-REVERIFIES-AT-HEAD G11).
    """
    symbols = _extract_missing_test_symbols(finding.claim)
    if not symbols:
        return FindingStatus.PROPOSED, "symbol_search", "no checkable symbol"
    targets = _missing_test_search_targets(symbols)
    search = find_tests_by_name(repo_root, targets, config.test_roots)
    if search.found:
        target, where = search.found[0]
        return FindingStatus.REFUTED, "symbol_search", f"{target} exists at {where}"
    return (
        FindingStatus.VERIFIED,
        "symbol_search",
        f"no test for {', '.join(symbols)} ({search.scope()})",
    )


def _verify_missing_docstring(
    finding: Finding, repo_root: Path, config: GatesConfig
) -> tuple[FindingStatus, str, str]:
    """Refute a missing-docstring claim when every public symbol is documented.

    Files the project exempts from docstring rules
    (``[gates].docstring_exempt``; repoach's own ``tests/``, ``scripts/``
    and ``src/repoach/llm_proxy/`` by default) are refuted outright — a
    missing-docstring claim there contradicts the project's own policy
    (SP-VERIFIER-LIB precision, surfaced by the SP-MERGE-GATE-SHADOW
    dry-run on PR #378).
    """
    if config.docstring_exempt and finding.file.startswith(config.docstring_exempt):
        return FindingStatus.REFUTED, "ast_docstring", "path is docstring-exempt (pyproject D)"
    reader = make_repo_file_reader(repo_root)
    source = reader(finding.file)
    if source is None:
        return FindingStatus.PROPOSED, "ast_docstring", "file unreadable"
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return FindingStatus.PROPOSED, "ast_docstring", "file unparseable"
    undocumented = 0
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if node.name.startswith("_"):
            continue
        if ast.get_docstring(node) is None:
            undocumented += 1
    module_documented = ast.get_docstring(tree) is not None
    if module_documented and undocumented == 0:
        return FindingStatus.REFUTED, "ast_docstring", "all public symbols documented"
    return FindingStatus.VERIFIED, "ast_docstring", f"{undocumented} public symbol(s) undocumented"


def _verify_lint(
    finding: Finding, repo_root: Path, config: GatesConfig
) -> tuple[FindingStatus, str, str]:
    """Verify a lint-convention claim by running the repo's own scanners."""
    target = repo_root / finding.file
    if not target.is_file():
        return FindingStatus.PROPOSED, "lint_scan", "file absent"
    violations = len(scan_inline(target)) + len(scan_silent(target))
    if violations:
        return FindingStatus.VERIFIED, "lint_scan", f"{violations} violation(s)"
    return FindingStatus.REFUTED, "lint_scan", "file clean"


_MECHANICAL = {
    ClaimType.MISSING_TEST: _verify_missing_test,
    ClaimType.MISSING_DOCSTRING: _verify_missing_docstring,
    ClaimType.LINT_CONVENTION: _verify_lint,
}

_PYTHON_ONLY: frozenset[ClaimType] = frozenset(
    {ClaimType.MISSING_DOCSTRING, ClaimType.LINT_CONVENTION}
)
"""Claim types whose verifier parses Python (``ast`` / the house scanners).

They run only when the project declares Python and the cited file is a
``.py`` under its source or test roots; anywhere else they end
``not_applicable`` with method ``language`` (SP-UNIVERSAL-GATES G3)."""


def verify_finding(
    finding: Finding, *, repo_root: Path, config: GatesConfig | None = None
) -> tuple[FindingStatus, str, str]:
    """Return ``(new_status, method, result)`` for one finding.

    Mechanically-checkable claim types dispatch to their verifier;
    everything else (``design`` / ``security`` / ``broken_behavior`` /
    ``spec_gap``) is left ``proposed`` for the judged slices. A
    Python-only verifier on a file it does not apply to returns
    ``NOT_APPLICABLE`` with method ``language`` and the reason
    (``not python: .tsx``, ``python not declared``, ``outside src_roots``).

    Args:
        finding: The proposed finding to check.
        repo_root: Root the cited paths/symbols resolve against (the
            PR head checkout the orchestrator already uses).
        config: The project's gate configuration; read from
            ``repo_root`` when omitted.

    Returns:
        A ``(FindingStatus, method, result)`` triple; the status is
        ``PROPOSED`` when the claim type is deferred or the evidence
        could not be read.
    """
    verifier = _MECHANICAL.get(finding.claim_type)
    if verifier is None:
        return FindingStatus.PROPOSED, "deferred", _DEFERRED_RESULT
    resolved = config if config is not None else load_gates_config(repo_root)
    if finding.claim_type in _PYTHON_ONLY:
        ok, reason = applicable(finding.file, resolved)
        if not ok:
            return FindingStatus.NOT_APPLICABLE, _LANGUAGE_METHOD, reason
    return verifier(finding, repo_root, resolved)


def verify_findings_for_pr(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str | None,
    config: GatesConfig | None = None,
) -> dict[str, int]:
    """Verify every proposed finding of a PR and persist the transitions.

    Args:
        db_path: The findings ledger database.
        pr_number: PR whose proposed findings are checked.
        repo_root: Root the verifiers resolve evidence against.
        head_sha: Commit the verification ran against (stored on each
            updated row); ``None`` becomes ``""``.
        config: The trusted gate configuration (the checkout's, never the
            PR head's); read from ``repo_root`` when omitted.

    Returns:
        Counts ``{"verified": n, "refuted": n, "deferred": n}``, plus
        ``"not_applicable": n`` when any finding ended not applicable.
    """
    counts = {"verified": 0, "refuted": 0, "deferred": 0}
    config = config if config is not None else load_gates_config(repo_root)
    proposed = fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED)
    for finding in proposed:
        new_status, method, result = verify_finding(finding, repo_root=repo_root, config=config)
        if new_status is FindingStatus.PROPOSED:
            if finding.id is not None:
                record_verification_attempt(
                    db_path,
                    finding.id,
                    method=method,
                    result=result,
                    checked_at_sha=head_sha or "",
                )
            counts["deferred"] += 1
            continue
        if finding.id is None:
            continue
        update_finding_status(
            db_path,
            finding.id,
            new_status,
            verification_method=method,
            verification_result=result,
            checked_at_sha=head_sha or "",
        )
        counts[new_status.value] = counts.get(new_status.value, 0) + 1
    return counts
