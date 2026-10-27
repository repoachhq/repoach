"""The rows of the generated-artefact flow and what is said about them.

The types the evidence, the ``generated_checks`` table and the findings
share (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ), with the helpers that
group runs and name their cause. A check's output is pull-request text:
:func:`tail_lines` and :func:`last_line` make it inert before it is
stored or shown.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal

from ..project.config import GENERATED_OUTPUT_TAIL_LINES

GENERATED_FINDER = "generated_check"
"""Finder and verification method of a ``generated_artefact`` finding."""

CAUSE_LINE_MAX_CHARS = 200
"""Characters of a check's last output line a finding's cause keeps."""

CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

MARKUP_RE = re.compile(r"[`<>\[\]|@]")


class GeneratedState(StrEnum):
    """The state of one check run (one ``generated_checks`` row)."""

    VERIFIED = "generated_artefact_verified"
    STALE = "generated_artefact_stale"
    NO_SOURCE_CHANGED = "generated_artefact_no_source_changed"
    CHECK_ABSENT = "generated_check_absent"
    SKIPPED_COST = "skipped (cost)"
    NOT_RUN = "generated_check_not_run"


ScriptRef = Literal["head", "base"]


@dataclass(frozen=True)
class GeneratedCheck:
    """The outcome of one check run against the head tree.

    Attributes:
        path: The artefact's path.
        argv: The declared check, placeholders replaced.
        head_sha: The head the check ran against.
        artefact_sha256: The artefact's sha256 at that head.
        sources_changed: Source paths the diff changes.
        state: The run's :class:`GeneratedState`.
        exit_code: The check's exit code; ``None`` when it did not exit.
        duration_ms: Wall time of the run.
        output_tail: The last :data:`GENERATED_OUTPUT_TAIL_LINES` lines of output,
            or the reason it did not run.
        script_ref: ``base`` for the run with the base-ref script (D3).
        producer_inputs_changed: Producer-input paths the diff changes.
        runs_for_path: How many runs the path had (2 under D3).
    """

    path: str
    argv: tuple[str, ...]
    head_sha: str
    artefact_sha256: str
    sources_changed: tuple[str, ...]
    state: GeneratedState
    exit_code: int | None
    duration_ms: int
    output_tail: str
    script_ref: ScriptRef = "head"
    producer_inputs_changed: tuple[str, ...] = ()
    runs_for_path: int = 1

    def as_json(self) -> dict[str, Any]:
        """The evidence entry of this run."""
        return {
            "path": self.path,
            "argv": list(self.argv),
            "head_sha": self.head_sha,
            "artefact_sha256": self.artefact_sha256,
            "sources_changed": list(self.sources_changed),
            "producer_inputs_changed": list(self.producer_inputs_changed),
            "state": self.state.value,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "output_tail": self.output_tail,
            "script_ref": self.script_ref,
            "runs_for_path": self.runs_for_path,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> GeneratedCheck:
        """Rebuild a run from its evidence entry."""
        exit_code = data.get("exit_code")
        return cls(
            path=str(data["path"]),
            argv=tuple(str(arg) for arg in data["argv"]),
            head_sha=str(data["head_sha"]),
            artefact_sha256=str(data["artefact_sha256"]),
            sources_changed=tuple(str(p) for p in data.get("sources_changed", ())),
            state=GeneratedState(str(data["state"])),
            exit_code=None if exit_code is None else int(exit_code),
            duration_ms=int(data.get("duration_ms", 0)),
            output_tail=tail_lines(str(data.get("output_tail", ""))),
            script_ref="base" if data.get("script_ref") == "base" else "head",
            producer_inputs_changed=tuple(str(p) for p in data.get("producer_inputs_changed", ())),
            runs_for_path=int(data.get("runs_for_path", 1)),
        )


@dataclass(frozen=True)
class GeneratedTiming:
    """Wall time of ``repoach review generated`` (G7).

    Attributes:
        checks_ms: Sum of the check runs.
        overhead_ms: Everything else: reading the declarations, the head
            tree, writing the evidence.
    """

    checks_ms: int
    overhead_ms: int

    def line(self) -> str:
        """The report line ``generated checks: <n> ms + <m> ms overhead``."""
        return f"generated checks: {self.checks_ms} ms + {self.overhead_ms} ms overhead"


TAIL_LINE_MAX_CHARS = 1000
"""Characters one kept output line may hold."""


def tail_lines(output: str) -> str:
    """Keep the last lines of a check's output, bounded and inert.

    The last :data:`GENERATED_OUTPUT_TAIL_LINES` non-empty lines, control
    characters removed, each cut at :data:`TAIL_LINE_MAX_CHARS`. Applied
    where the output is captured and again where the review reads the
    evidence, which the job running pull-request code wrote.

    Args:
        output: Raw output, or a tail read from the evidence.

    Returns:
        At most ``GENERATED_OUTPUT_TAIL_LINES`` lines of at most
        ``TAIL_LINE_MAX_CHARS`` characters.
    """
    lines = [CONTROL_RE.sub("", line)[:TAIL_LINE_MAX_CHARS] for line in output.splitlines()]
    lines = [line for line in lines if line.strip()]
    return "\n".join(lines[-GENERATED_OUTPUT_TAIL_LINES:])


def last_line(tail: str) -> str:
    """The last line of an output tail, made inert: the check's output is pull-request text.

    Control and Markdown markup characters become spaces and the line is
    cut at :data:`CAUSE_LINE_MAX_CHARS`, so the cause a finding stores and
    the report prints cannot carry a mention, a link or an escape sequence.
    """
    lines = tail.splitlines()
    if not lines:
        return ""
    plain = MARKUP_RE.sub(" ", CONTROL_RE.sub(" ", lines[-1]))
    return " ".join(plain.split())[:CAUSE_LINE_MAX_CHARS]


def check_cause(check: GeneratedCheck) -> str:
    """Name why ``check`` does not show the derivation (the finding's ``verification_result``).

    Args:
        check: A run that did not verify the derivation.

    Returns:
        The cause a finding stores as its ``verification_result``.
    """
    if check.state is GeneratedState.NO_SOURCE_CHANGED:
        return "no source changed"
    if check.state is GeneratedState.STALE:
        prefix = f"{check.script_ref} " if check.runs_for_path > 1 else ""
        if check.exit_code is None:
            return f"{prefix}check failed: {last_line(check.output_tail)}"
        return f"{prefix}check exited {check.exit_code}: {last_line(check.output_tail)}"
    if check.state is GeneratedState.CHECK_ABSENT and check.argv:
        return f"check script {check.argv[0]} absent at {check.head_sha[:7]}"
    return last_line(check.output_tail) or check.state.value


def by_path(checks: Sequence[GeneratedCheck]) -> dict[str, list[GeneratedCheck]]:
    """Group runs by artefact path, keeping the order of first appearance.

    Args:
        checks: Runs of any artefacts.

    Returns:
        ``{path: its runs}``.
    """
    grouped: dict[str, list[GeneratedCheck]] = {}
    for check in checks:
        grouped.setdefault(check.path, []).append(check)
    return grouped


def path_verified(runs: Sequence[GeneratedCheck]) -> bool:
    """Whether every run of one path verified the derivation (a source changed, exit 0).

    Args:
        runs: Every run of one artefact.

    Returns:
        True when there is at least one run and each verified.
    """
    return bool(runs) and all(run.state is GeneratedState.VERIFIED for run in runs)


__all__ = [
    "CAUSE_LINE_MAX_CHARS",
    "CONTROL_RE",
    "GENERATED_FINDER",
    "MARKUP_RE",
    "TAIL_LINE_MAX_CHARS",
    "GeneratedCheck",
    "GeneratedState",
    "GeneratedTiming",
    "ScriptRef",
    "by_path",
    "check_cause",
    "last_line",
    "path_verified",
    "tail_lines",
]
