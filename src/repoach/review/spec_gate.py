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
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import (
    Column,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    select,
)
from sqlalchemy.engine import Engine

from ..core.logging import get_logger
from ..core.sqlite_schema_init import ensure_schema_created
from .plan import ActionPlan, load_plan, parse_plan_markdown, plan_relpath

_log = get_logger(__name__)

_metadata = MetaData()

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
)


class SpecCoverage(BaseModel):
    """Presence report for a plan's promised acceptance selectors.

    ``covered`` is true only when the plan promised at least one
    selector and every promised selector is present in the head.

    Attributes:
        spec_id: The spec whose plan is being checked.
        n_promised: Number of selectors promised by the plan.
        n_present: Number of selectors present in the head.
        missing: List of selectors that are absent.
        covered: True iff promised ≥ 1 and nothing missing.
    """

    spec_id: str
    n_promised: int
    n_present: int
    missing: list[str]
    covered: bool


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
        trailing name is not defined.
    """
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

    Returns:
        ``True`` for a bare file selector (no body to inspect), or for
        a node id whose trailing function has at least one non-trivial
        statement, or when the file fails to parse (a syntax error is
        a separate, unrelated concern from an empty promise). ``False``
        when the file is absent, the trailing name is not defined, or
        its body is empty / ``pass`` / ``...`` / a bare docstring only.
    """
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


def compute_spec_coverage(repo_root: Path, *, spec_id: str, plan: ActionPlan) -> SpecCoverage:
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

    Returns:
        A :class:`SpecCoverage` whose ``covered`` is ``True`` only when
        the plan promised at least one selector and every promised
        selector is present, with a non-trivial body, in the head.
    """
    selectors = acceptance_selectors(plan)
    missing = [
        s
        for s in selectors
        if not (selector_present(repo_root, s) and promised_body_non_trivial(repo_root, s))
    ]
    n_present = len(selectors) - len(missing)
    covered = bool(selectors) and not missing
    return SpecCoverage(
        spec_id=spec_id,
        n_promised=len(selectors),
        n_present=n_present,
        missing=missing,
        covered=covered,
    )


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
    relpath = plan_relpath(spec_id)
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
    """Create the ``pr_spec_coverage`` table if it does not exist (idempotent)."""
    ensure_schema_created(_engine_for(db_path), _metadata)


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
            )
            for row in rows
        ]
