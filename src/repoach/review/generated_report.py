"""What the review says about generated artefacts: the JSON entries and the report lines (G5, G7).

``review pr`` prints them on stderr and in its JSON, ``review generated``
on stderr, and the Markdown report fences them
(SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .generated_types import GeneratedCheck, GeneratedState, GeneratedTiming, by_path, check_cause


def report_json(checks: Sequence[GeneratedCheck]) -> list[dict[str, Any]]:
    """The ``generated`` array of the ``review pr`` JSON (G5).

    Args:
        checks: The runs of this head.

    Returns:
        One JSON entry per run.
    """
    return [
        {
            "path": check.path,
            "check": " ".join(check.argv),
            "state": check.state.value,
            "ms": check.duration_ms,
            "sources_changed": list(check.sources_changed),
            "producer_inputs_changed": list(check.producer_inputs_changed),
            "script_ref": check.script_ref,
        }
        for check in checks
    ]


def report_lines(
    checks: Sequence[GeneratedCheck], timing: GeneratedTiming | None = None
) -> list[str]:
    """The report lines naming what proved each artefact, or why nothing did (G5, G7).

    Args:
        checks: The runs of this head.
        timing: The measured delay; its line is printed when given and there were runs.

    Returns:
        The lines, in declaration order.
    """
    lines: list[str] = []
    for path, runs in by_path(checks).items():
        for run in runs:
            argv = " ".join(run.argv)
            if run.state is GeneratedState.VERIFIED:
                where = f"{run.script_ref} script, " if run.runs_for_path > 1 else ""
                lines.append(f"generated: {path} verified by {argv} ({where}{run.duration_ms} ms)")
            else:
                lines.append(f"generated: {path} {run.state.value}: {check_cause(run)}")
        producers = runs[0].producer_inputs_changed
        if producers:
            lines.append(f"producer inputs changed: {', '.join(producers)}")
    if timing is not None and checks:
        lines.append(timing.line())
    return lines


__all__ = ["report_json", "report_lines"]
