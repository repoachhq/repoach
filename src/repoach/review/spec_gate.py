"""Spec-coverage presence check (SP-SPEC-GATE, review redesign slice 6).

The redesign turns "the diff covers the spec" from a Tester opinion
into an executed fact. Execution of the spec's acceptance criteria
(running the promised tests) belongs in the trusted merge context —
the review job is isolated from PR-code execution by
SP-CI-SECRETS-ISOLATION. This slice does the half that is safe in the
review job: it records whether the plan's promised acceptance
selectors (its steps' ``unit_tests`` plus the plan ``integration_tests``)
are PRESENT in the PR head — the file exists and, when a ``::test``
node id is given, the symbol is defined. Combined with CI-green and
the slice-7 gate (which executes them), present + passing is the full
coverage story. Pure data-only reads; dual-run; no merge decision
changes here.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine

from ..arch import path_contains
from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from ..gates.selectors import SelectorKind, TestSelector, parse_selector, selector_file
from ..gates.selectors import selector_present as gate_selector_present
from .governed_spec import (
    GovernedSpec,
    SpecRefUnavailableError,
    load_governed_spec,
    load_governed_spec_at_ref,
)
from .plan import ActionPlan, load_plan, parse_plan_markdown, plan_relpath

_log = get_logger(__name__)

_metadata = MetaData()

SPEC_COVERAGE_RULE_VERSION = 1
"""Version of the rule that graded a coverage row (SP-PLAN-SELECTOR-AUDIT-WIRE 0.3).

``0`` marks a row recorded before the marker existed, by code that knew
neither planned selectors nor the ``origin/<base>`` retry; ``1`` is
today's rule. ``review gate`` recomputes a row at head whose version is
below this one (:func:`refresh_stale_coverage`), so a row graded by
retired code can never refuse on its own. Bump it whenever the grading
of a selector changes.
"""

_pr_spec_coverage = Table(
    "pr_spec_coverage",
    _metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("pr_number", Integer, nullable=False),
    Column("head_sha", String, nullable=False),
    Column("spec_id", String, nullable=False),
    Column("n_promised", Integer, nullable=False),
    Column("n_present", Integer, nullable=False),
    Column("missing", String, nullable=False),
    Column("covered", Integer, nullable=False),
    Column("planned", String, nullable=False, server_default=""),
    Column("draft_refusals", String, nullable=False, server_default=""),
    Column("rule_version", Integer, nullable=False, server_default="0"),
)

_ADDED_COLUMNS = (
    ("planned", "TEXT NOT NULL DEFAULT ''"),
    ("draft_refusals", "TEXT NOT NULL DEFAULT ''"),
    ("rule_version", "INTEGER NOT NULL DEFAULT 0"),
)
"""Columns added after the table first shipped (SP-PLAN-SELECTOR-AUDIT-WIRE 0.2, 0.3)."""

_REFUSAL_SEPARATOR = "\n"
"""Joins ``draft_refusals`` lines in storage; a refusal line itself holds commas."""


class SpecCoverage(BaseModel):
    """Presence report for a plan's promised acceptance selectors.

    ``covered`` is true only when the plan promised at least one
    selector and every promised selector is present in the head.

    Attributes:
        spec_id: The spec whose plan is being checked.
        n_promised: Number of selectors promised by the plan.
        n_present: Number of selectors present in the head.
        missing: List of selectors that are absent.
        covered: True iff promised ≥ 1, nothing missing and no draft
            refusal.
        planned: Selectors of a pre-implementation spec whose file does
            not exist at head yet — reported, never refusing.
        draft_refusals: One line per rule that withheld the draft
            exemption (owned code touched, spec past draft at base).
        rule_version: The :data:`SPEC_COVERAGE_RULE_VERSION` that graded
            the report; ``0`` for a row recorded before the marker.
    """

    spec_id: str
    n_promised: int
    n_present: int
    missing: list[str]
    covered: bool
    planned: list[str] = []
    draft_refusals: list[str] = []
    rule_version: int = SPEC_COVERAGE_RULE_VERSION


def acceptance_selectors(plan: ActionPlan) -> list[str]:
    """Return the plan's promised pytest selectors, de-duplicated in order.

    Args:
        plan: The loaded action plan whose steps' ``unit_tests`` and
            plan-level ``integration_tests`` are the acceptance contract.

    Returns:
        The unit-test selectors of every step followed by the
        integration selectors, first-occurrence order, no duplicates.
    """
    seen: set[str] = set()
    ordered: list[str] = []
    for step in plan.steps:
        for selector in step.unit_tests:
            if selector not in seen:
                seen.add(selector)
                ordered.append(selector)
    for selector in plan.integration_tests:
        if selector not in seen:
            seen.add(selector)
            ordered.append(selector)
    return ordered


def promised_present(repo_root: Path, selector: str) -> bool:
    r"""Return whether *selector*'s trailing function name is defined at head.

    The promise is satisfied by any ``def <name>(`` or
    ``async def <name>(`` in the file at any indentation (flat or
    class-nested), regardless of intermediate class segments. A
    word-boundary regex (``(?:async\s+)?def\s+NAME\s*\(``) replaces
    the previous substring scan, so a promise ``test_foo`` is no
    longer satisfied by ``def test_foobar(`` and a class-scoped
    promise no longer requires every intermediate ``class <C>`` to be
    present (SP-DEV-PROMISE-TRAILING-NAME, 2026-07-10;
    SP-GATE-ASYNC-DEF-SELECTOR, 2026-07-18).

    Args:
        repo_root: Root the selector path resolves against (the PR head).
        selector: A pytest selector — a bare ``file.py``, a
            ``file.py::test_name`` node id, or a class-scoped
            ``file.py::TestClass::test_name`` node id (any ``[param]``
            suffix is stripped before the symbol search).

    Returns:
        ``True`` when the file exists and, for a node id, the file
        defines ``def <trailing_name>(`` or
        ``async def <trailing_name>(`` at any indentation; ``False``
        when the file is absent, the file cannot be read, or the
        trailing name is not defined. Vitest, Go and ``cmd:``
        selectors are judged by
        :func:`repoach.gates.selectors.selector_present` instead
        (SP-UNIVERSAL-GATES G8).
    """
    parsed = parse_selector(selector)
    if parsed is not None and parsed.kind is not SelectorKind.PYTEST:
        return gate_selector_present(repo_root, parsed)
    file_part, _, node = selector.partition("::")
    target = repo_root / file_part
    if not target.is_file():
        return False
    if not node:
        return True
    segments = [part.split("[", 1)[0] for part in node.split("::") if part]
    if not segments:
        return False
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("spec_gate.selector_read_failed", selector=selector, error=str(exc)[:120])
        return False
    pattern = r"(?m)^\s*(?:async\s+)?def\s+" + re.escape(segments[-1]) + r"\s*\("
    return re.search(pattern, source) is not None


def selector_present(repo_root: Path, selector: str) -> bool:
    """Return whether *selector*'s file (and node-id symbols) exist at head.

    Args:
        repo_root: Root the selector path resolves against (the PR head).
        selector: A pytest selector — a bare ``file.py``, a
            ``file.py::test_name`` node id, or a class-scoped
            ``file.py::TestClass::test_name`` node id (any ``[param]``
            suffix is stripped before the symbol search).

    Returns:
        ``True`` when the file exists and, for a node id, the file
        defines ``def <trailing_name>(`` at any indentation (flat or
        class-nested); ``False`` when the file or the trailing name is
        absent or the file cannot be read. Delegates to
        :func:`promised_present` so every caller (the merge gate and
        the self-verify unit-missing check) shares the same
        word-boundary, class-tolerant predicate
        (SP-DEV-PROMISE-TRAILING-NAME, 2026-07-10).
    """
    return promised_present(repo_root, selector)


def _orphaned(repo_root: Path, selector: str, action: str | None) -> bool:
    """Whether one promised selector is orphaned at head (see :func:`audit_plan_selectors`).

    A ``cmd:`` selector is orphaned whenever it names a command the
    project did not declare in ``[commands]`` (nothing can create it);
    a Go selector whose package directory does not exist yet is exempt
    like an undelivered test file.
    """
    parsed = parse_selector(selector)
    if parsed is not None and parsed.kind is SelectorKind.COMMAND:
        return not gate_selector_present(repo_root, parsed)
    if parsed is not None and parsed.kind is SelectorKind.GO:
        package = repo_root / (parsed.file.removesuffix("...").rstrip("/") or ".")
        if not package.is_dir() or gate_selector_present(repo_root, parsed):
            return False
        return action is None or parsed.name not in action
    file_part = selector_file(selector)
    node = selector[len(file_part) :].lstrip(":> ").strip()
    if not (repo_root / file_part).is_file():
        return False
    if selector_present(repo_root, selector):
        return False
    return action is None or not node or node not in action


def audit_plan_selectors(plan: ActionPlan, repo_root: Path) -> list[str]:
    """Return every promised selector orphaned relative to the tree at head.

    A selector is orphaned when its file already exists at *repo_root*
    but the selector does not satisfy :func:`selector_present` and,
    for a step-scoped selector, its node id is not declared verbatim in
    the promising step's ``action`` text. A selector whose file does
    not yet exist is exempt — the file is an undelivered deliverable,
    not a drift. Checks every ``step.unit_tests`` entry and every
    ``plan.integration_tests`` entry (plan-level selectors have no
    declared-creation exemption, matching the existing planner-time
    check).

    Args:
        plan: The plan whose promised selectors to audit.
        repo_root: Repository root the selectors resolve against.

    Returns:
        The offending selectors, in plan order; empty when every
        promised selector is either undelivered-and-exempt or
        satisfied/declared.
    """
    offenders: list[str] = []
    for step in plan.steps:
        for selector in step.unit_tests:
            if _orphaned(repo_root, selector, step.action):
                offenders.append(selector)
    for selector in plan.integration_tests:
        if _orphaned(repo_root, selector, None):
            offenders.append(selector)
    return offenders


def _body_is_trivial(body: list[ast.stmt]) -> bool:
    """Return whether every statement in *body* is a no-op placeholder.

    A statement is a no-op placeholder when it is ``pass``, a bare
    ``...`` expression, or a bare string literal (a docstring with no
    accompanying code). Any other statement (an ``assert``, a call, a
    ``return``, ...) makes the body non-trivial.
    """
    for stmt in body:
        if isinstance(stmt, ast.Pass):
            continue
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and (stmt.value.value is Ellipsis or isinstance(stmt.value.value, str))
        ):
            continue
        return False
    return True


_SKIPPED_TEST_MODIFIERS = frozenset({"todo", "skip"})
"""Vitest modifiers whose block never runs: ``it.todo(...)``, ``it.skip(...)``."""


def _skip_literal(source: str, index: int) -> int:
    """Return the index just past the string or comment starting at ``index``.

    Returns ``index`` unchanged when no string literal or comment starts there.
    """
    char = source[index]
    if char in "'\"`":
        cursor = index + 1
        while cursor < len(source) and source[cursor] != char:
            cursor += 2 if source[cursor] == "\\" else 1
        return cursor + 1
    if source.startswith("//", index):
        end = source.find("\n", index)
        return len(source) if end == -1 else end
    if source.startswith("/*", index):
        end = source.find("*/", index + 2)
        return len(source) if end == -1 else end + 2
    return index


def _code_between(
    source: str, start: int, opener: str, closer: str, *, keep_strings: bool = True
) -> str | None:
    """Return the code between the bracket at ``start`` and its match, comments dropped.

    Strings are kept (they are code) unless ``keep_strings`` is False,
    when each is replaced by a space; comments are removed. ``None``
    when the bracket is never closed.
    """
    depth = 0
    kept: list[str] = []
    index = start
    while index < len(source):
        after = _skip_literal(source, index)
        if after != index:
            if source[index] in "'\"`":
                kept.append(source[index:after] if keep_strings else " ")
            index = after
            continue
        char = source[index]
        if char == opener:
            depth += 1
        elif char == closer:
            depth -= 1
            if depth == 0:
                return "".join(kept)[1:]
        kept.append(char)
        index += 1
    return None


_VITEST_BLOCK = (
    r"\b(?P<call>it|test|describe)(?P<modifiers>(?:\.\w+)*)\s*(?P<paren>\()\s*"
    r"(?P<quote>['\"`])"
)
"""The head of a Vitest block up to its title's opening quote."""

_ARROW_HEAD = re.compile(r",\s*(?:async\s+)?(?:\([^)]*\)|[A-Za-z_$][\w$]*)\s*=>\s*")
_FUNCTION_HEAD = re.compile(r",\s*(?:async\s+)?function\b[^{]*")


def _skipped(match: re.Match[str]) -> bool:
    """Whether a matched Vitest block carries ``.todo`` or ``.skip``."""
    modifiers = {part for part in match.group("modifiers").split(".") if part}
    return bool(modifiers & _SKIPPED_TEST_MODIFIERS)


def _callback_body(source: str, match: re.Match[str], *, keep_strings: bool = True) -> str | None:
    """Return the code of the callback following a matched Vitest title, comments dropped.

    ``match`` ends right after the title's closing quote. A ``{…}`` body
    returns its content; an expression-bodied arrow returns the
    expression; a call with no callback returns ``None``.
    """
    rest = source[match.end() :]
    arrow = _ARROW_HEAD.match(rest)
    function = _FUNCTION_HEAD.match(rest)
    head = arrow or function
    if head is None:
        return None
    offset = match.end() + head.end()
    if offset < len(source) and source[offset] == "{":
        return _code_between(source, offset, "{", "}", keep_strings=keep_strings)
    if function is not None:
        return None
    return _code_between("(" + source[offset:], 0, "(", ")", keep_strings=keep_strings)


def vitest_test_bodies(source: str) -> list[str]:
    """Return the callback code of every ``it`` / ``test`` block that runs.

    ``describe`` blocks, ``.todo`` / ``.skip`` blocks and blocks with no
    callback are left out; comments and string literals are dropped
    from each body, so a title, a ``// TODO`` or a quoted name never
    counts as code (SP-GATE-REVERIFIES-AT-HEAD).

    Args:
        source: A Vitest test file.

    Returns:
        One code string per running test block, in source order.
    """
    pattern = re.compile(_VITEST_BLOCK + r"(?:\\.|(?!(?P=quote))[^\\])*(?P=quote)\s*")
    bodies: list[str] = []
    for match in pattern.finditer(source):
        if match.group("call") == "describe" or _skipped(match):
            continue
        body = _callback_body(source, match, keep_strings=False)
        if body is not None and body.strip():
            bodies.append(body)
    return bodies


def vitest_body_non_trivial(repo_root: Path, selector: TestSelector) -> bool:
    """Return whether a Vitest selector's block has a non-empty callback body.

    The block is the first ``it`` / ``test`` / ``describe`` call (any
    ``.only`` / ``.concurrent`` chain) whose title is the selector's
    trailing name. Its callback must hold code once comments are
    dropped: an arrow function with a non-empty ``{…}`` body or an
    expression body, or a ``function`` with a non-empty body. A call
    with no callback, an ``it.todo`` / ``it.skip``, or ``() => {}``
    is trivial. A whole-file selector is non-trivial when the file
    exists.

    Args:
        repo_root: Root the selector path resolves against (the PR head).
        selector: A parsed Vitest selector.

    Returns:
        Whether the promised test has a body that runs code.
    """
    target = repo_root / selector.file
    if not target.is_file():
        return False
    if not selector.names:
        return True
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("spec_gate.vitest_read_failed", selector=selector.raw, error=str(exc)[:120])
        return False
    pattern = re.compile(_VITEST_BLOCK + re.escape(selector.name) + r"(?P=quote)\s*")
    match = pattern.search(source)
    if match is None or _skipped(match):
        return False
    body = _callback_body(source, match)
    return body is not None and bool(body.strip())


def promised_body_non_trivial(repo_root: Path, selector: str) -> bool:
    r"""Return whether *selector*'s promised test has a non-trivial body.

    Complements :func:`promised_present`'s presence-only check with a
    structural body check, kept as a separate predicate so the
    presence-only callers (the dev preflight, the planner's
    step-satisfied check, the self-verify unit-missing check) are
    unaffected and keep resolving selectors via :func:`promised_present`
    / :func:`selector_present` exactly as before. Only
    :func:`compute_spec_coverage` composes the two, so a hollow ``def
    test_x(): pass`` (or an ellipsis-only / docstring-only body) does
    not satisfy the acceptance contract even though the file and
    symbol are present (SP-SPEC-CONTRACT-BASE, G3).

    Args:
        repo_root: Root the selector path resolves against.
        selector: A pytest selector, as accepted by
            :func:`promised_present`.

    Non-pytest selectors are dispatched through
    :func:`~repoach.gates.selectors.parse_selector` exactly as
    :func:`promised_present` dispatches them: a Vitest selector (written
    with `` > `` or ``::``) is judged by its ``it`` / ``test`` /
    ``describe`` block (:func:`vitest_body_non_trivial`); Go and ``cmd:``
    selectors have no body to inspect. A node id in a file that is not
    Python never reaches Python's ``ast``, so a TypeScript parse error
    can never pass for a non-empty body (SP-GATE-REVERIFIES-AT-HEAD G6,
    tech_debt row 60).

    Returns:
        ``True`` for a bare file selector (no body to inspect), or for
        a node id whose trailing function has at least one non-trivial
        statement, or when a Python file fails to parse (a syntax error
        is a separate, unrelated concern from an empty promise).
        ``False`` when the file is absent, the trailing name is not
        defined, its body is empty / ``pass`` / ``...`` / a bare
        docstring only, or the node id names a test in a file that is
        neither Python nor a recognised Vitest test file.
    """
    parsed = parse_selector(selector)
    if parsed is not None and parsed.kind is SelectorKind.VITEST:
        return vitest_body_non_trivial(repo_root, parsed)
    if parsed is not None and parsed.kind is not SelectorKind.PYTEST:
        return True
    file_part, _, node = selector.partition("::")
    target = repo_root / file_part
    if not target.is_file():
        return False
    if not node:
        return True
    if not file_part.endswith(".py"):
        return False
    segments = [part.split("[", 1)[0] for part in node.split("::") if part]
    if not segments:
        return False
    try:
        source = target.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        _log.debug("spec_gate.body_read_failed", selector=selector, error=str(exc)[:120])
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        _log.debug("spec_gate.body_parse_failed", selector=selector, error=str(exc)[:120])
        return True
    name = segments[-1]
    for candidate in ast.walk(tree):
        if (
            isinstance(candidate, (ast.FunctionDef, ast.AsyncFunctionDef))
            and candidate.name == name
        ):
            return not _body_is_trivial(candidate.body)
    return False


def compute_spec_coverage(
    repo_root: Path, *, spec_id: str, plan: ActionPlan, draft: DraftScope | None = None
) -> SpecCoverage:
    """Build the presence coverage report for *plan* against the head tree.

    *plan* supplies the GRADED acceptance contract — the caller loads
    it from the PR's BASE ref (``develop``) via :func:`resolve_contract_
    plan` / :func:`load_plan_from_ref`, so a PR cannot weaken the
    selectors it is judged against by editing its own
    ``docs/plans/<id>.md`` (SP-SPEC-CONTRACT-BASE). *repo_root* is
    always the PR head: every base-contract selector's presence AND
    non-trivial body are checked there, so the PR must still actually
    add the promised test (G2).

    Args:
        repo_root: The PR head checkout the selectors resolve against.
        spec_id: The spec whose plan is being checked (recorded on the
            report).
        plan: The loaded action plan supplying the acceptance
            selectors — the BASE-ref plan in normal operation.
        draft: The draft scope when the spec is pre-implementation (see
            :func:`coverage_of_selectors`), else ``None``.

    Returns:
        A :class:`SpecCoverage` whose ``covered`` is ``True`` only when
        the plan promised at least one selector and every promised
        selector is present, with a non-trivial body, in the head.
    """
    return coverage_of_selectors(
        repo_root, spec_id=spec_id, selectors=acceptance_selectors(plan), draft=draft
    )


def coverage_of_selectors(
    repo_root: Path,
    *,
    spec_id: str,
    selectors: list[str],
    draft: DraftScope | None = None,
) -> SpecCoverage:
    """Grade *selectors* against the head tree, honouring a draft scope.

    A selector that is present with a non-trivial body counts as present.
    Any other selector is ``missing``, except one whose FILE is absent at
    head while *draft* allows planned selectors: that one is ``planned``
    (SP-PLAN-SELECTOR-AUDIT-WIRE 0.2). A selector whose file exists but
    whose test does not is always missing, draft or not.

    Args:
        repo_root: The PR head checkout the selectors resolve against.
        spec_id: The spec recorded on the report.
        selectors: The acceptance selectors, in order.
        draft: The draft scope of a pre-implementation spec, or ``None``.

    Returns:
        The :class:`SpecCoverage`; ``covered`` holds when at least one
        selector is promised, none is missing and *draft* names no
        refusal.
    """
    allow_planned = draft is not None and draft.allows_planned
    missing: list[str] = []
    planned: list[str] = []
    for selector in selectors:
        if selector_present(repo_root, selector) and promised_body_non_trivial(repo_root, selector):
            continue
        if allow_planned and not (repo_root / selector_file(selector)).exists():
            planned.append(selector)
        else:
            missing.append(selector)
    refusals = list(draft.refusals) if draft is not None else []
    return SpecCoverage(
        spec_id=spec_id,
        n_promised=len(selectors),
        n_present=len(selectors) - len(missing) - len(planned),
        missing=missing,
        covered=bool(selectors) and not missing and not refusals,
        planned=planned,
        draft_refusals=refusals,
    )


PRE_IMPLEMENTATION_STATUSES = frozenset({"draft", "proposed"})
"""Frontmatter statuses of a spec whose implementation has not started."""


@dataclass(frozen=True)
class DraftScope:
    """Whether a pre-implementation spec's absent selectors may be planned.

    Attributes:
        status: The spec's status at head (one of
            :data:`PRE_IMPLEMENTATION_STATUSES`).
        refusals: One line per rule that withholds the exemption — the PR
            touches code the spec owns, or the spec is past draft at base.
    """

    status: str
    refusals: tuple[str, ...] = ()

    @property
    def allows_planned(self) -> bool:
        """Whether absent selectors are reported as planned rather than missing."""
        return not self.refusals


def draft_scope(
    *,
    head_status: str,
    base_status: str | None,
    owns_code: tuple[str, ...],
    changed_files: list[str],
    spec_path: str | None,
) -> DraftScope | None:
    """Build the draft scope of a spec, or ``None`` when it is not a draft.

    The exemption is for a PR that only writes documentation: it is
    withheld when the PR changes any path outside :data:`DOCS_ROOT`
    other than the spec file itself, when it changes a path under
    :data:`DOCS_ROOT` that the spec owns (head or base ``owns``), and
    when the spec exists at base with a status past draft — a PR cannot
    step its own spec back to draft to escape its selectors.

    Args:
        head_status: The spec's frontmatter status at the PR head.
        base_status: Its status at the base ref, ``None`` when the base
            does not carry the spec (a new spec).
        owns_code: The union of the head and base ``owns.code`` paths.
        changed_files: The paths the PR diff touches.
        spec_path: The spec's repo-relative path, never counted as owned code.

    Returns:
        ``None`` when *head_status* is not pre-implementation; else the scope.
    """
    if head_status not in PRE_IMPLEMENTATION_STATUSES:
        return None
    refusals: list[str] = []
    if base_status is not None and base_status not in PRE_IMPLEMENTATION_STATUSES:
        refusals.append(
            f"spec is {base_status!r} at base: a {head_status} status at head "
            "does not make its selectors planned"
        )
    others = sorted(path for path in changed_files if path != spec_path)
    outside_docs = [path for path in others if not path_contains(DOCS_ROOT, path)]
    if outside_docs:
        refusals.append(
            f"{head_status} spec PR touches code outside {DOCS_ROOT} "
            f"({', '.join(outside_docs)}): its acceptance selectors must be present"
        )
    owned_in_docs = [
        path
        for path in others
        if path_contains(DOCS_ROOT, path) and any(path_contains(owned, path) for owned in owns_code)
    ]
    if owned_in_docs:
        refusals.append(
            f"{head_status} spec PR touches owned code ({', '.join(owned_in_docs)}): "
            "its acceptance selectors must be present"
        )
    return DraftScope(status=head_status, refusals=tuple(refusals))


DOCS_ROOT = "docs"
"""The only tree a draft spec PR may change and keep its planned selectors."""


_AC_HEADING = re.compile(r"(?im)^##\s+acceptance criteria\s*$")
_NEXT_HEADING = re.compile(r"(?m)^##\s")
_PYTEST_SPAN = re.compile(r"`\s*pytest\s([^`]*)`")
_PYTEST_TARGET = re.compile(r"^[\w./-]+\.py(?:::[\w\[\]-]+)*$")


def spec_acceptance_selectors(markdown: str) -> list[str]:
    """Return the pytest selectors a spec's Acceptance Criteria name, de-duplicated.

    Reads the ``## Acceptance Criteria`` section only, and only the
    backticked ``pytest [flags] <file.py>[::node...]`` invocations in it — the
    contract a spec without a committed plan is graded against.

    Args:
        markdown: The full spec markdown.

    Returns:
        The selectors in first-occurrence order; empty when the section
        is absent or names no pytest selector.
    """
    heading = _AC_HEADING.search(markdown)
    if heading is None:
        return []
    following = _NEXT_HEADING.search(markdown, heading.end())
    section = markdown[heading.end() : following.start() if following else len(markdown)]
    ordered: list[str] = []
    for span in _PYTEST_SPAN.finditer(section):
        for token in span.group(1).split():
            if _PYTEST_TARGET.match(token) and token not in ordered:
                ordered.append(token)
    return ordered


class BaseRefUnavailableError(RuntimeError):
    """*ref* itself does not resolve to a commit in the local clone.

    Raised by :func:`load_plan_from_ref` when the git revision cannot
    be resolved at all — as opposed to resolving but carrying no plan
    file. The caller MUST fail CLOSED on this (SP-SPEC-CONTRACT-BASE
    Failure scenarios: treat coverage as NOT covered), never falling
    back to the PR-head contract, which is exactly the attackable path
    this spec closes.
    """


def load_plan_from_ref(spec_id: str, ref: str, *, root: Path | None = None) -> ActionPlan:
    """Load and parse *spec_id*'s committed plan as it exists at *ref*.

    Reads the plan via ``git show <ref>:docs/plans/<SPEC-ID>.md``
    against the working tree at *root* — no checkout, no working-tree
    mutation, so the PR-head checkout backing the review job is left
    untouched. This is the base-ref half of SP-SPEC-CONTRACT-BASE: the
    caller passes the PR's base ref (``develop``) so the acceptance
    contract graded by :func:`compute_spec_coverage` is the one the PR
    cannot itself have edited.

    Args:
        spec_id: Spec identifier whose plan to read.
        ref: A git revision — branch name, remote-tracking ref
            (``origin/develop``), or commit — to read the plan from.
        root: Repository root the git commands run against (defaults
            to ``Path.cwd()``).

    Returns:
        The validated plan as committed at *ref*.

    Raises:
        BaseRefUnavailableError: *ref* itself does not resolve to a
            commit in *root*.
        FileNotFoundError: *ref* resolves but carries no
            ``docs/plans/<SPEC-ID>.md`` — a genuinely new spec/plan
            introduced only on the PR head.
        ValueError: The file exists at *ref* but fails to parse (see
            :func:`repoach.review.plan.parse_plan_markdown`).
    """
    base = (root or Path.cwd()).resolve()
    verify = subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    if verify.returncode != 0:
        raise BaseRefUnavailableError(
            f"base ref {ref!r} does not resolve to a commit in {base}: "
            f"{verify.stderr.strip()[:200]}"
        )
    relpath = plan_relpath(spec_id, root=base)
    show = subprocess.run(
        ["git", "show", f"{ref}:{relpath}"],
        cwd=base,
        capture_output=True,
        text=True,
        check=False,
    )
    if show.returncode != 0:
        raise FileNotFoundError(
            f"no committed plan at {relpath!r} on ref {ref!r}: {show.stderr.strip()[:200]}"
        )
    return parse_plan_markdown(show.stdout)


@dataclass(frozen=True)
class ContractResolution:
    """Outcome of resolving the BASE-ref acceptance-contract plan.

    Attributes:
        plan: The plan to grade coverage against — the base plan when
            *base_available* is True, the head plan when
            *fell_back_to_head* is also True, or ``None`` when the
            base ref could not be resolved at all (the caller must
            fail CLOSED rather than grade against anything).
        base_available: Whether *base_ref* resolved to a commit.
            ``False`` is the fail-CLOSED trigger (SP-SPEC-CONTRACT-BASE
            Failure scenarios): the caller must still record a
            NOT-covered report, never skip recording.
        fell_back_to_head: True when the base ref resolved but carried
            no plan for this spec — a spec/plan introduced only on the
            PR head — so the head plan is graded instead (logged by
            the caller as a fallback, SP-SPEC-CONTRACT-BASE Edge cases).
    """

    plan: ActionPlan | None
    base_available: bool
    fell_back_to_head: bool


def resolve_contract_plan(
    spec_id: str,
    *,
    repo_root: Path,
    base_ref: str | None,
) -> ContractResolution:
    """Resolve the acceptance-contract plan to grade *spec_id* against.

    Tries :func:`load_plan_from_ref` against *base_ref* first. A
    genuinely new spec/plan (present at head, absent at base) falls
    back to the head plan for THIS call only, per the spec's documented
    first-introduction policy. A *base_ref* that does not resolve at
    all is reported as unavailable and NOT retried here — the caller
    (which owns the git remote / fetch policy) decides whether to fetch
    and call this again with a remote-tracking ref.

    Args:
        spec_id: The spec whose plan is the acceptance contract.
        repo_root: The PR-head checkout — used for the head-plan
            fallback and passed through to the base-ref git commands.
        base_ref: The git ref to read the base plan from (a branch
            name or an already-fetched ``origin/<branch>``), or
            ``None`` when the PR's base ref could not be determined.

    Returns:
        A :class:`ContractResolution`.
    """
    if not base_ref:
        return ContractResolution(plan=None, base_available=False, fell_back_to_head=False)
    try:
        plan = load_plan_from_ref(spec_id, base_ref, root=repo_root)
        return ContractResolution(plan=plan, base_available=True, fell_back_to_head=False)
    except BaseRefUnavailableError as exc:
        _log.warning(
            "spec_gate.base_ref_unavailable",
            spec_id=spec_id,
            base_ref=base_ref,
            error=str(exc)[:200],
        )
        return ContractResolution(plan=None, base_available=False, fell_back_to_head=False)
    except FileNotFoundError as exc:
        _log.info(
            "spec_gate.base_plan_absent_fallback_to_head",
            spec_id=spec_id,
            base_ref=base_ref,
            error=str(exc)[:200],
        )
        try:
            head_plan = load_plan(spec_id, root=repo_root)
        except (FileNotFoundError, ValueError) as head_exc:
            _log.info(
                "spec_gate.head_plan_also_unavailable",
                spec_id=spec_id,
                error=str(head_exc)[:200],
            )
            return ContractResolution(plan=None, base_available=True, fell_back_to_head=True)
        return ContractResolution(plan=head_plan, base_available=True, fell_back_to_head=True)


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine, creating the parent directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_spec_coverage_schema(db_path: Path) -> None:
    """Create the ``pr_spec_coverage`` table if absent and add its later columns (idempotent)."""
    engine = _engine_for(db_path)
    ensure_schema_created(engine, _metadata)
    inspector = inspect(engine)
    if not inspector.has_table("pr_spec_coverage"):
        return
    existing = {col["name"] for col in inspector.get_columns("pr_spec_coverage")}
    with engine.begin() as conn:
        for column, ddl in _ADDED_COLUMNS:
            if column not in existing:
                conn.execute(text(f"ALTER TABLE pr_spec_coverage ADD COLUMN {column} {ddl}"))


def record_spec_coverage(
    db_path: Path, *, pr_number: int, head_sha: str | None, coverage: SpecCoverage
) -> None:
    """Persist one coverage report for a PR.

    Args:
        db_path: Path to the SQLite database.
        pr_number: The PR number being reviewed.
        head_sha: The commit SHA of the PR head.
        coverage: The coverage report to persist.
    """
    init_spec_coverage_schema(db_path)
    engine = _engine_for(db_path)
    with engine.begin() as conn:
        conn.execute(
            _pr_spec_coverage.insert().values(
                pr_number=pr_number,
                head_sha=head_sha or "",
                spec_id=coverage.spec_id,
                n_promised=coverage.n_promised,
                n_present=coverage.n_present,
                missing=",".join(coverage.missing),
                covered=int(coverage.covered),
                planned=",".join(coverage.planned),
                draft_refusals=_REFUSAL_SEPARATOR.join(coverage.draft_refusals),
                rule_version=coverage.rule_version,
            )
        )


def fetch_spec_coverage(
    db_path: Path, pr_number: int, *, head_sha: str | None = None
) -> list[SpecCoverage]:
    """Return recorded coverage reports for a PR, ordered by id.

    Args:
        db_path: Path to the SQLite database.
        pr_number: The PR number to fetch reports for.
        head_sha: When given, only reports recorded at this exact head
            are returned — the merge gate pins coverage to the decided
            head so a stale ``covered=True`` from an earlier push can
            never carry the gate (SP-GATE-JUDGED-FAIL-CLOSED, audit
            finding M8).

    Returns:
        List of matching coverage reports, ordered by insertion id.
    """
    engine = _engine_for(db_path)
    stmt = (
        select(_pr_spec_coverage)
        .where(_pr_spec_coverage.c.pr_number == pr_number)
        .order_by(_pr_spec_coverage.c.id)
    )
    if head_sha is not None:
        stmt = stmt.where(_pr_spec_coverage.c.head_sha == head_sha)
    with engine.connect() as conn:
        rows = list(conn.execute(stmt).mappings())
        return [
            SpecCoverage(
                spec_id=row["spec_id"],
                n_promised=row["n_promised"],
                n_present=row["n_present"],
                missing=[m for m in row["missing"].split(",") if m],
                covered=bool(row["covered"]),
                planned=[p for p in row["planned"].split(",") if p],
                draft_refusals=[r for r in row["draft_refusals"].split(_REFUSAL_SEPARATOR) if r],
                rule_version=row["rule_version"],
            )
            for row in rows
        ]


def draft_scope_at_head(
    repo_root: Path, spec_id: str, *, base_ref: str | None, changed_files: list[str]
) -> tuple[DraftScope | None, str]:
    """Return the draft scope of *spec_id* at head, and the spec's markdown.

    ``(None, "")`` when the spec is not governed or its head status is
    not pre-implementation. The base view of the spec is read from
    *base_ref*, then from ``origin/<base_ref>``; when neither resolves
    the scope carries a refusal, so an unreadable base never grants
    the exemption (fail closed).

    Args:
        repo_root: The PR head tree.
        spec_id: The spec under review.
        base_ref: The PR's base branch, or ``None`` when unknown.
        changed_files: The paths the PR diff touches.

    Returns:
        The scope (or ``None``) and the spec's markdown at head.
    """
    head = load_governed_spec(spec_id, root=repo_root)
    if head is None or head.path is None:
        return None, ""
    spec_path = head.path.resolve().relative_to(repo_root.resolve()).as_posix()
    base: GovernedSpec | None = None
    base_unreadable = True
    for ref in (base_ref, f"origin/{base_ref}") if base_ref else ():
        try:
            base = load_governed_spec_at_ref(spec_id, ref, root=repo_root)
        except SpecRefUnavailableError as exc:
            _log.info("review_team.draft_scope_ref_unavailable", ref=ref, error=str(exc)[:200])
            continue
        base_unreadable = False
        break
    scope = draft_scope(
        head_status=head.status,
        base_status=base.status if base is not None else None,
        owns_code=tuple(dict.fromkeys((*head.owns_code, *(base.owns_code if base else ())))),
        changed_files=changed_files,
        spec_path=spec_path,
    )
    if scope is None:
        return None, ""
    if base_unreadable:
        scope = replace(
            scope,
            refusals=(
                *scope.refusals,
                f"base ref {base_ref!r} unreadable: the spec's draft status cannot be checked",
            ),
        )
    return scope, head.path.read_text(encoding="utf-8")


def coverage_at_head(
    repo_root: Path,
    *,
    spec_id: str,
    base_ref: str | None,
    changed_files: list[str],
    fetch_base: Callable[[str], None] | None = None,
) -> tuple[SpecCoverage, ContractResolution]:
    """Grade *spec_id*'s acceptance selectors at the head tree, without any model call.

    A pure function of the spec and plan (base contract first), the head
    tree and the diff's paths: a fresh review, a replayed review and the
    gate all reach the same report through it. When *base_ref* does not
    resolve locally, *fetch_base* is asked to fetch it and the contract is
    read from ``origin/<base_ref>``.

    Args:
        repo_root: The PR head tree the selectors resolve against.
        spec_id: The spec under review.
        base_ref: The PR's base branch, or ``None`` when unknown.
        changed_files: The paths the PR diff touches.
        fetch_base: Fetches ``origin/<base_ref>`` into *repo_root*.

    Returns:
        The coverage report and the contract resolution it used.
    """
    resolution = resolve_contract_plan(spec_id, repo_root=repo_root, base_ref=base_ref)
    if not resolution.base_available and base_ref and fetch_base is not None:
        fetch_base(base_ref)
        resolution = resolve_contract_plan(
            spec_id, repo_root=repo_root, base_ref=f"origin/{base_ref}"
        )
    draft, spec_markdown = draft_scope_at_head(
        repo_root, spec_id, base_ref=base_ref, changed_files=changed_files
    )
    if resolution.plan is not None:
        coverage = compute_spec_coverage(
            repo_root, spec_id=spec_id, plan=resolution.plan, draft=draft
        )
    elif resolution.base_available and draft is not None:
        coverage = coverage_of_selectors(
            repo_root,
            spec_id=spec_id,
            selectors=spec_acceptance_selectors(spec_markdown),
            draft=draft,
        )
    else:
        coverage = SpecCoverage(
            spec_id=spec_id,
            n_promised=0,
            n_present=0,
            missing=["plan-absent" if resolution.base_available else "base-ref-unavailable"],
            covered=False,
        )
    return coverage, resolution


class StaleCoverageRecomputeError(RuntimeError):
    """A stale coverage row could not be recomputed; the stored row stands.

    Attributes:
        head_sha: The head whose row is stale.
        rule_version: The ``rule_version`` of the stored row.
        reason: The gate reason naming the failure and what stands.
    """

    def __init__(self, *, head_sha: str, rule_version: int, error: Exception) -> None:
        self.head_sha = head_sha
        self.rule_version = rule_version
        self.reason = (
            f"spec coverage recompute failed at {head_sha[:7]}: {type(error).__name__}: "
            f"{str(error)[:200]}; stored verdict from rule {rule_version} stands"
        )
        super().__init__(self.reason)


def refresh_stale_coverage(
    db_path: Path,
    *,
    pr_number: int,
    head_sha: str,
    repo_root: Path,
    base_ref: str | None,
    changed_files: Callable[[], list[str]],
    fetch_base: Callable[[str], None] | None = None,
) -> SpecCoverage | None:
    """Recompute and record the coverage at *head_sha* when its latest row is stale.

    A row is stale when its ``rule_version`` is below
    :data:`SPEC_COVERAGE_RULE_VERSION`: it was graded by code whose rule
    has since changed, so it no longer says what the current rule says
    about the same head (SP-PLAN-SELECTOR-AUDIT-WIRE 0.3). The spec is the
    row's own; *repo_root* must be the tree of *head_sha*.

    Args:
        db_path: The coverage ledger.
        pr_number: The PR under decision.
        head_sha: The decided head.
        repo_root: The materialised tree of *head_sha*.
        base_ref: The PR's base branch, or ``None`` when unknown.
        changed_files: Returns the paths the PR diff touches; called only
            when a row is stale.
        fetch_base: Fetches ``origin/<base_ref>`` when the base does not
            resolve locally.

    Returns:
        The recorded report, or ``None`` when no row at head is stale.

    Raises:
        StaleCoverageRecomputeError: The diff, the base fetch or the
            grading failed; nothing was recorded.
    """
    init_spec_coverage_schema(db_path)
    rows = fetch_spec_coverage(db_path, pr_number, head_sha=head_sha)
    if not rows or rows[-1].rule_version >= SPEC_COVERAGE_RULE_VERSION:
        return None
    stale = rows[-1]
    try:
        coverage, _ = coverage_at_head(
            repo_root,
            spec_id=stale.spec_id,
            base_ref=base_ref,
            changed_files=changed_files(),
            fetch_base=fetch_base,
        )
    except Exception as exc:
        raise StaleCoverageRecomputeError(
            head_sha=head_sha, rule_version=stale.rule_version, error=exc
        ) from exc
    record_spec_coverage(db_path, pr_number=pr_number, head_sha=head_sha, coverage=coverage)
    _log.info(
        "spec_gate.stale_coverage_recomputed",
        pr_number=pr_number,
        head_sha=head_sha[:12],
        spec_id=stale.spec_id,
        stale_rule_version=stale.rule_version,
        covered=coverage.covered,
        n_planned=len(coverage.planned),
        n_missing=len(coverage.missing),
    )
    return coverage
