"""``repoach spec`` — reports over the spec corpus (SP-APPROVED-SPEC-WITHOUT-PLAN-GRADES-ITS-ACS).

``repoach spec audit --plans`` lists every spec past draft that has no
committed plan, with the number of selectors its Acceptance Criteria
name — the contract the merge gate grades it against — so plans or AC
selector lines can be backfilled by area. It also lists, per committed
plan, the steps over the Planner's step caps, which the gate does not
enforce. A report, never a gate: it exits 0 except for a usage error
(``audit`` without ``--plans``), which exits 2.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import typer

from ..arch import load_registry
from ..core.logging import get_logger
from ..review.plan import (
    PLAN_MARKER,
    PLAN_STEP_MAX_FILES,
    PLAN_STEP_MAX_UNIT_SELECTORS,
    parse_plan_markdown,
    plan_relpath,
)
from ..review.spec import specs_dir_for
from ..review.spec_gate import PRE_IMPLEMENTATION_STATUSES, spec_acceptance_selectors
from .doctor_cmds import project_root_or_exit

_log = get_logger(__name__)

spec_app = typer.Typer(help="Reports over the spec corpus.", no_args_is_help=True)


@dataclass(frozen=True)
class PlanlessSpec:
    """A spec past draft with no committed plan.

    Attributes:
        id: The SP-ID.
        status: Its frontmatter status.
        selectors: The selectors its Acceptance Criteria name.
        owns_code: Its ``owns.code`` paths.
    """

    id: str
    status: str
    selectors: int
    owns_code: tuple[str, ...]


@dataclass(frozen=True)
class OversizedStep:
    """A committed plan step over the Planner's step caps.

    Attributes:
        id: The spec whose plan it is.
        step: The step index.
        files: Files the step touches.
        selectors: Unit selectors the step promises.
    """

    id: str
    step: int
    files: int
    selectors: int


def _sort_key(spec: PlanlessSpec) -> tuple[int, str, str]:
    """Sort by first owned code path, specs owning none last, then by id."""
    if spec.owns_code:
        return (0, spec.owns_code[0], spec.id)
    return (1, "", spec.id)


@spec_app.command("audit")
def spec_audit(
    plans: bool = typer.Option(
        False, "--plans", help="List the specs past draft without a committed plan."
    ),
    as_json: bool = typer.Option(False, "--json", help="Print one JSON object."),
) -> None:
    """Report the specs past draft without a plan, and the plan steps over the caps.

    Fields within a line are separated by one TAB character, written
    ``<TAB>`` below.

    - One line per spec past draft (any status outside draft /
      proposed) with no action plan (a plan file without the marker is
      no plan), sorted by first ``owns.code`` path then id:
      ``<id><TAB><status><TAB><n> selectors<TAB><owns.code joined by "," or ->``.
    - Then ``<N> specs past draft without a plan (<M> with no selector to grade)``.
    - Then one line per committed plan step over ``PLAN_STEP_MAX_FILES``
      or ``PLAN_STEP_MAX_UNIT_SELECTORS``:
      ``<id><TAB>step <n><TAB><files>/<cap> files<TAB><selectors>/<cap> selectors``.
    - Then ``<id><TAB>not an action plan: <path>`` per plan file without
      the marker, and ``<id><TAB>plan does not parse: <error>`` per
      marked plan that does not parse (the only plan the gate refuses on).

    ``--json`` prints ``{"specs", "total", "no_selectors",
    "oversized_steps", "unparseable_plans", "not_action_plans"}``.

    Exits 0 (a report, not a gate), and 2 when ``--plans`` is missing:
    it is the only audit.
    """
    if not plans:
        typer.echo("repoach: spec audit needs --plans (the only audit)", err=True)
        raise typer.Exit(code=2)
    root = project_root_or_exit()
    registry = load_registry(specs_dir_for(root), tolerate_malformed=True)
    planless: list[PlanlessSpec] = []
    oversized: list[OversizedStep] = []
    unparseable: list[tuple[str, str]] = []
    not_action: list[tuple[str, str]] = []
    for node in sorted(registry.nodes.values(), key=lambda n: n.id):
        if node.frontier:
            continue
        relpath = plan_relpath(node.id, root=root)
        plan_path = root / relpath
        plan_text = plan_path.read_text(encoding="utf-8") if plan_path.is_file() else None
        if plan_text is not None and PLAN_MARKER not in plan_text:
            not_action.append((node.id, relpath))
            plan_text = None
        if plan_text is not None:
            try:
                plan = parse_plan_markdown(plan_text)
            except ValueError as exc:
                _log.info("spec_audit.plan_unparseable", spec_id=node.id, error=str(exc)[:200])
                unparseable.append((node.id, str(exc)[:200]))
                continue
            oversized.extend(
                OversizedStep(node.id, step.index, len(step.files), len(step.unit_tests))
                for step in plan.steps
                if len(step.files) > PLAN_STEP_MAX_FILES
                or len(step.unit_tests) > PLAN_STEP_MAX_UNIT_SELECTORS
            )
            continue
        if node.status in PRE_IMPLEMENTATION_STATUSES:
            continue
        selectors = spec_acceptance_selectors(node.path.read_text(encoding="utf-8"))
        planless.append(PlanlessSpec(node.id, node.status, len(selectors), node.owns_code))
    planless.sort(key=_sort_key)
    no_selectors = sum(1 for spec in planless if spec.selectors == 0)
    if as_json:
        typer.echo(
            json.dumps(
                {
                    "specs": [
                        {
                            "id": spec.id,
                            "status": spec.status,
                            "selectors": spec.selectors,
                            "owns_code": list(spec.owns_code),
                        }
                        for spec in planless
                    ],
                    "total": len(planless),
                    "no_selectors": no_selectors,
                    "oversized_steps": [
                        {
                            "id": step.id,
                            "step": step.step,
                            "files": step.files,
                            "files_cap": PLAN_STEP_MAX_FILES,
                            "selectors": step.selectors,
                            "selectors_cap": PLAN_STEP_MAX_UNIT_SELECTORS,
                        }
                        for step in oversized
                    ],
                    "unparseable_plans": [
                        {"id": spec_id, "error": error} for spec_id, error in unparseable
                    ],
                    "not_action_plans": [
                        {"id": spec_id, "path": path} for spec_id, path in not_action
                    ],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return
    for spec in planless:
        owned = ",".join(spec.owns_code) or "-"
        typer.echo(f"{spec.id}\t{spec.status}\t{spec.selectors} selectors\t{owned}")
    typer.echo(
        f"{len(planless)} specs past draft without a plan ({no_selectors} with no selector to grade)"
    )
    for step in oversized:
        typer.echo(
            f"{step.id}\tstep {step.step}\t{step.files}/{PLAN_STEP_MAX_FILES} files\t"
            f"{step.selectors}/{PLAN_STEP_MAX_UNIT_SELECTORS} selectors"
        )
    for spec_id, path in not_action:
        typer.echo(f"{spec_id}\tnot an action plan: {path}")
    for spec_id, error in unparseable:
        typer.echo(f"{spec_id}\tplan does not parse: {error}")
