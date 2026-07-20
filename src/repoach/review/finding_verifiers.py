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
from pathlib import Path

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
    make_repo_symbol_searcher,
)

_DOCSTRING_EXEMPT_PREFIXES = ("tests/", "scripts/", "src/repoach/llm_proxy/")
"""Paths the project exempts from docstring (ruff ``D``) rules in
pyproject.toml — a missing-docstring claim on them is a false positive
by the project's own policy (SP-VERIFIER-LIB precision, surfaced by the
SP-MERGE-GATE-SHADOW dry-run on PR #378)."""

_DEFERRED_RESULT = "not mechanically verifiable — slices 5-6"


def _verify_missing_test(finding: Finding, repo_root: Path) -> tuple[FindingStatus, str, str]:
    """Refute a missing-test claim when the symbol already exists in the repo."""
    symbols = _extract_missing_test_symbols(finding.claim)
    if not symbols:
        return FindingStatus.PROPOSED, "symbol_search", "no checkable symbol"
    searcher = make_repo_symbol_searcher(repo_root)
    found = [s for s in symbols if searcher(s)]
    if found:
        return FindingStatus.REFUTED, "symbol_search", f"{found[0]} exists in tests/src"
    return FindingStatus.VERIFIED, "symbol_search", f"no test for {', '.join(symbols)}"


def _verify_missing_docstring(finding: Finding, repo_root: Path) -> tuple[FindingStatus, str, str]:
    """Refute a missing-docstring claim when every public symbol is documented.

    Files the project exempts from docstring rules
    (:data:`_DOCSTRING_EXEMPT_PREFIXES`) are refuted outright — a
    missing-docstring claim there contradicts the project's own policy.
    """
    if finding.file.startswith(_DOCSTRING_EXEMPT_PREFIXES):
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


def _verify_lint(finding: Finding, repo_root: Path) -> tuple[FindingStatus, str, str]:
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


def verify_finding(finding: Finding, *, repo_root: Path) -> tuple[FindingStatus, str, str]:
    """Return ``(new_status, method, result)`` for one finding.

    Mechanically-checkable claim types dispatch to their verifier;
    everything else (``design`` / ``security`` / ``broken_behavior`` /
    ``spec_gap``) is left ``proposed`` for the judged slices.

    Args:
        finding: The proposed finding to check.
        repo_root: Root the cited paths/symbols resolve against (the
            PR head checkout the orchestrator already uses).

    Returns:
        A ``(FindingStatus, method, result)`` triple; the status is
        ``PROPOSED`` when the claim type is deferred or the evidence
        could not be read.
    """
    verifier = _MECHANICAL.get(finding.claim_type)
    if verifier is None:
        return FindingStatus.PROPOSED, "deferred", _DEFERRED_RESULT
    return verifier(finding, repo_root)


def verify_findings_for_pr(
    db_path: Path,
    *,
    pr_number: int,
    repo_root: Path,
    head_sha: str | None,
) -> dict[str, int]:
    """Verify every proposed finding of a PR and persist the transitions.

    Args:
        db_path: The findings ledger database.
        pr_number: PR whose proposed findings are checked.
        repo_root: Root the verifiers resolve evidence against.
        head_sha: Commit the verification ran against (stored on each
            updated row); ``None`` becomes ``""``.

    Returns:
        Counts ``{"verified": n, "refuted": n, "deferred": n}``.
    """
    counts = {"verified": 0, "refuted": 0, "deferred": 0}
    proposed = fetch_findings(db_path, pr_number, status=FindingStatus.PROPOSED)
    for finding in proposed:
        new_status, method, result = verify_finding(finding, repo_root=repo_root)
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
        counts[new_status.value] += 1
    return counts
