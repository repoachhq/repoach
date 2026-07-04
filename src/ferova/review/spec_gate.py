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
from .plan import ActionPlan

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
        defines ``def <function>`` plus ``class <C>`` for every
        intermediate class segment; ``False`` when the file or any
        symbol is absent or the file cannot be read. The whole node
        was previously treated as one function name, so every
        class-scoped promised selector failed the self-verify and
        spec-coverage checks even while green (SP-DEV-STEP-PREFLIGHT,
        2026-07-04).
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
    if f"def {segments[-1]}" not in source:
        return False
    return all(f"class {cls}" in source for cls in segments[:-1])


def compute_spec_coverage(repo_root: Path, *, spec_id: str, plan: ActionPlan) -> SpecCoverage:
    """Build the presence coverage report for *plan* against the head tree.

    Args:
        repo_root: The PR head checkout the selectors resolve against.
        spec_id: The spec whose plan is being checked (recorded on the
            report).
        plan: The loaded action plan supplying the acceptance selectors.

    Returns:
        A :class:`SpecCoverage` whose ``covered`` is ``True`` only when
        the plan promised at least one selector and every promised
        selector is present in the head.
    """
    selectors = acceptance_selectors(plan)
    missing = [s for s in selectors if not selector_present(repo_root, s)]
    n_present = len(selectors) - len(missing)
    covered = bool(selectors) and not missing
    return SpecCoverage(
        spec_id=spec_id,
        n_promised=len(selectors),
        n_present=n_present,
        missing=missing,
        covered=covered,
    )


def _engine_for(db_path: Path) -> Engine:
    """Return a SQLite engine, creating the parent directory if needed."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    return create_engine(f"sqlite:///{db_path}")


def init_spec_coverage_schema(db_path: Path) -> None:
    """Create the ``pr_spec_coverage`` table if it does not exist (idempotent)."""
    _metadata.create_all(_engine_for(db_path), checkfirst=True)


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


def fetch_spec_coverage(db_path: Path, pr_number: int) -> list[SpecCoverage]:
    """Return every recorded coverage report for a PR, ordered by id.

    Args:
        db_path: Path to the SQLite database.
        pr_number: The PR number to fetch reports for.

    Returns:
        List of coverage reports for the PR, ordered by insertion id.
    """
    engine = _engine_for(db_path)
    stmt = (
        select(_pr_spec_coverage)
        .where(_pr_spec_coverage.c.pr_number == pr_number)
        .order_by(_pr_spec_coverage.c.id)
    )
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
