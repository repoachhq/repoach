"""The rows of the generated-artefact flow and what is said about them.

The types the evidence, the ``generated_checks`` table and the findings
share (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ), with the helpers that
group runs and name their cause. A check's output is pull-request text:
:func:`tail_lines` and :func:`last_line` make it inert before it is
stored or shown.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from ..project.config import GENERATED_CHECK_TIMEOUT_S, GENERATED_OUTPUT_TAIL_LINES
from ..project.globs import glob_matches

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


@dataclass(frozen=True)
class GeneratedEntry:
    """One declared generated artefact (a ``[[review.generated]]`` entry read at the base).

    Attributes:
        path: Glob of the artefact, repository-relative.
        check: The argv proving the derivation; ``check[0]`` is a script.
        sources: Globs of the files the artefact derives from (non-empty).
        producer_inputs: Globs of files pinning the producer (D1); they
            count as sources and are reported apart.
        check_timeout_s: Seconds the check may run.
    """

    path: str
    check: tuple[str, ...]
    sources: tuple[str, ...]
    producer_inputs: tuple[str, ...] = ()
    check_timeout_s: int = GENERATED_CHECK_TIMEOUT_S

    def matches(self, path: str) -> bool:
        """Whether ``path`` is this artefact."""
        return glob_matches(self.path, path)

    def argv_line(self) -> str:
        """The declared check, joined by spaces, as the report prints it."""
        return " ".join(self.check)


def changed_inputs(
    entry: GeneratedEntry, path: str, diff_files: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(sources changed, producer inputs changed)`` for one artefact.

    The artefact itself never counts as its own source, and a path that
    is a source is not listed again as a producer input.

    Args:
        entry: The artefact's declaration.
        path: The artefact's changed path.
        diff_files: The paths the diff changes.

    Returns:
        The changed sources and the changed producer inputs, in diff order.
    """
    sources = tuple(
        f for f in diff_files if f != path and any(glob_matches(g, f) for g in entry.sources)
    )
    producers = tuple(
        f
        for f in diff_files
        if f != path and f not in sources and any(glob_matches(g, f) for g in entry.producer_inputs)
    )
    return sources, producers


def sha256_hex(content: bytes | None) -> str:
    """Return the hex sha256 of ``content``, empty for an absent file."""
    return hashlib.sha256(content).hexdigest() if content is not None else ""


def declared_argv(entry: GeneratedEntry, head_sha: str, base_sha: str) -> tuple[str, ...]:
    """The declared check with ``{head}`` / ``{base}`` replaced by the SHAs."""
    return tuple(arg.replace("{head}", head_sha).replace("{base}", base_sha) for arg in entry.check)


def symlinked(head_dir: Path, path: str) -> bool:
    """Whether ``path``, or a directory on its way, is a symlink in the head tree.

    Args:
        head_dir: The head tree.
        path: A repository-relative path (no ``..``).

    Returns:
        True when reading or writing ``path`` would follow a symlink.
    """
    current = head_dir
    for part in Path(path).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def symlink_row(
    entry: GeneratedEntry, path: str, *, head_sha: str, changed_files: Sequence[str]
) -> GeneratedCheck:
    """The stale run of an artefact the head commits as a symlink: never read, never green.

    The review job holds the secrets, so it reads no file a pull request
    can point at the host (a token file, a device) through a link.
    """
    sources, producers = changed_inputs(entry, path, changed_files)
    return GeneratedCheck(
        path=path,
        argv=entry.check,
        head_sha=head_sha,
        artefact_sha256="",
        sources_changed=sources,
        state=GeneratedState.STALE,
        exit_code=None,
        duration_ms=0,
        output_tail=f"artefact {path} is a symlink; it is not read",
        producer_inputs_changed=producers,
    )


def plain_file(head_dir: Path, path: str) -> bool:
    """Whether ``path`` is a regular file of the head tree, reached without a symlink.

    The guard for every read of a pull request's file (the artefact the
    summary hashes) and every write into its tree (the D3 base script):
    a symlink, or a directory symlink on the way, would leave the tree.

    Args:
        head_dir: The head tree.
        path: A repository-relative path (no ``..``).

    Returns:
        True when ``path`` is a plain file reached without a symlink.
    """
    return not symlinked(head_dir, path) and (head_dir / path).is_file()


__all__ = [
    "CAUSE_LINE_MAX_CHARS",
    "CONTROL_RE",
    "GENERATED_FINDER",
    "MARKUP_RE",
    "TAIL_LINE_MAX_CHARS",
    "GeneratedCheck",
    "GeneratedEntry",
    "GeneratedState",
    "GeneratedTiming",
    "ScriptRef",
    "by_path",
    "changed_inputs",
    "check_cause",
    "declared_argv",
    "last_line",
    "path_verified",
    "plain_file",
    "sha256_hex",
    "symlink_row",
    "symlinked",
    "tail_lines",
]
