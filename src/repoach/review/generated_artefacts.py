"""Declared generated artefacts: read at the base, summarised for the reviewers, checked.

A file a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. The
repository declares such a file under ``[[review.generated]]`` with the
command that proves the derivation (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

This module reads the declarations at the base ref, replaces a touched
artefact's diff unit by one summary paragraph before any reviewer lens
sees it (:func:`summarise`), runs the checks for the secret-free
verification job (:func:`run_checks`) and builds the blocking
``generated_artefact`` findings (:func:`generated_findings`). The rest of
the flow lives beside it:

- :mod:`~repoach.review.generated_config`: the declarations, read at
  the base ref;
- :mod:`~repoach.review.generated_types`: the rows, the entry and the
  causes every module shares;
- :mod:`~repoach.review.generated_process`: how one check process runs;
- :mod:`~repoach.review.generated_verify`: ``repoach review generated``;
- :mod:`~repoach.review.generated_evidence`: the evidence file and how the
  review reads it back;
- :mod:`~repoach.review.generated_store` and
  :mod:`~repoach.review.generated_settle`: the ``generated_checks`` rows
  and how they settle findings of older heads, never through the refuter.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from ..core.logging import get_logger
from ..project.config import (
    GENERATED_CHECK_TIMEOUT_S,
)
from .diff_scoper import split_diff
from .findings import (
    ClaimType,
    Finding,
    FindingStatus,
    Severity,
    fetch_findings,
    init_findings_schema,
    record_finding,
)
from .findings_bridge import finding_fingerprint
from .generated_config import (
    declared_at_base,
    touched_artefacts,
)
from .generated_git import blob_at, merge_base
from .generated_process import Runner
from .generated_types import (
    GENERATED_CHECKS_TOTAL_BUDGET_S,
    GENERATED_FINDER,
    GeneratedCheck,
    GeneratedEntry,
    GeneratedState,
    GeneratedTiming,
    ScriptRef,
    by_path,
    changed_inputs,
    check_cause,
    declared_argv,
    path_verified,
    plain_file,
    sha256_hex,
    symlink_row,
    symlinked,
    tail_lines,
)
from .secret_env import scrubbed_env

_log = get_logger(__name__)


_CLAIM_PREFIX = "generated artefact not derived from its sources: "


def summary_paragraph(
    path: str,
    entry: GeneratedEntry,
    *,
    before: bytes | None,
    after: bytes | None,
    diff_files: Sequence[str],
    symlink: bool = False,
) -> str:
    """Build the one paragraph every reviewer lens sees instead of the artefact's diff (G2).

    Sizes, hashes and paths only (NG1, NG2).

    Args:
        path: The artefact's changed path.
        entry: Its declaration.
        before: Its content at the base, ``None`` when it is new.
        after: Its content at the head, ``None`` when the diff deletes it.
        diff_files: The paths the diff changes.
        symlink: The head commits the artefact as a symlink, which is not read.

    Returns:
        ``generated artefact <path>: <before> -> <after> bytes, sha256
        <before7> -> <after7>; sources changed in this diff: <list or
        none>; proved by: <check>``, plus the changed producer inputs.
    """
    size_before = f"{len(before)}" if before is not None else "absent"
    size_after = f"{len(after)}" if after is not None else "deleted"
    if symlink:
        size_after = "a symlink, not read"
    sha_before = sha256_hex(before)[:7] or "none"
    sha_after = sha256_hex(after)[:7] or "none"
    sources, producers = changed_inputs(entry, path, diff_files)
    paragraph = (
        f"generated artefact {path}: {size_before} -> {size_after} bytes, "
        f"sha256 {sha_before} -> {sha_after}; "
        f"sources changed in this diff: {', '.join(sources) or 'none'}; "
        f"proved by: {entry.argv_line()}"
    )
    if producers:
        paragraph += f"; producer inputs changed: {', '.join(producers)}"
    return paragraph


def summarise(
    diff: str,
    touched: Mapping[str, GeneratedEntry],
    *,
    base_blob: Callable[[str], bytes | None],
    head_blob: Callable[[str], bytes | None],
    symlinks: frozenset[str] = frozenset(),
) -> str:
    """Return ``diff`` with each touched artefact's unit replaced by its summary paragraph.

    The unit keeps its ``diff --git`` header so the path stays a changed
    path; nothing else of the artefact reaches a reviewer.

    Args:
        diff: The PR's unified diff.
        touched: The declared artefacts the diff changes.
        base_blob: Reads a path at the base.
        head_blob: Reads a path at the head.
        symlinks: The touched artefacts the head commits as symlinks.

    Returns:
        The diff the reviewers see; ``diff`` itself when nothing is touched.
    """
    if not touched:
        return diff
    units = split_diff(diff)
    if not units:
        return diff
    paths = [unit.path for unit in units]
    rebuilt: list[str] = []
    for unit in units:
        entry = touched.get(unit.path)
        if entry is None:
            rebuilt.append(unit.text)
            continue
        paragraph = summary_paragraph(
            unit.path,
            entry,
            before=base_blob(unit.path),
            after=None if unit.path in symlinks else head_blob(unit.path),
            diff_files=paths,
            symlink=unit.path in symlinks,
        )
        rebuilt.append(f"diff --git a/{unit.path} b/{unit.path}\n{paragraph}\n")
    return "".join(rebuilt)


@dataclass(frozen=True)
class ReviewScope:
    """What the review job derives from the declarations before any reviewer runs.

    Attributes:
        review_diff: The diff every reviewer lens sees (:func:`summarise`).
        touched: The declared artefacts the diff changes, in declaration order.
        changed: The paths the diff changes.
        base_sha: The merge base the diff was read from, computed here.
    """

    review_diff: str
    touched: Mapping[str, GeneratedEntry] = field(default_factory=dict)
    changed: tuple[str, ...] = ()
    base_sha: str = ""


def review_scope(head_dir: Path, diff: str, base_ref: str | None) -> ReviewScope:
    """Read the declarations at ``base_ref`` and summarise the touched artefacts (G1, G2).

    Args:
        head_dir: The PR head tree (its refs reach the base).
        diff: The PR's unified diff.
        base_ref: A ref of the base that resolves; ``None`` declares nothing.

    Returns:
        The scope; ``review_diff`` is ``diff`` when nothing declared is touched.

    Raises:
        ConfigError: The base declarations are invalid, or two entries
            match one changed path.
    """
    changed = tuple(unit.path for unit in split_diff(diff))
    touched = touched_artefacts(declared_at_base(head_dir, base_ref), changed)
    if not touched or base_ref is None:
        return ReviewScope(review_diff=diff, changed=changed)
    before = merge_base(head_dir, base_ref, "HEAD") or base_ref

    def head_blob(path: str) -> bytes | None:
        target = head_dir / path
        return target.read_bytes() if plain_file(head_dir, path) else None

    summarised = summarise(
        diff,
        touched,
        base_blob=lambda path: blob_at(head_dir, before, path),
        head_blob=head_blob,
        symlinks=frozenset(path for path in touched if symlinked(head_dir, path)),
    )
    return ReviewScope(review_diff=summarised, touched=touched, changed=changed, base_sha=before)


def _interpreted(argv: Sequence[str], head_dir: Path) -> list[str]:
    """Prefix the check's script with its interpreter (``.py`` python3, ``.sh`` bash)."""
    script = argv[0]
    if script.endswith(".py"):
        return ["python3", *argv]
    if script.endswith(".sh"):
        return ["bash", *argv]
    return [str(head_dir / script), *argv[1:]]


def _artefact_moved(tree: Path, path: str, bound_sha: str) -> bool:
    """Whether the run left ``path`` other than the blob the run was bound to."""
    if symlinked(tree, path) or not (tree / path).is_file():
        return True
    return sha256_hex((tree / path).read_bytes()) != bound_sha


class ScriptTamperedError(RuntimeError):
    """The check script became a symlink, or left the head tree, while the base script ran."""


@contextmanager
def _script_at(head_dir: Path, script: str, content: bytes | None) -> Iterator[None]:
    """Put ``content`` at ``script`` in the head tree for one run, then restore it (D3).

    The run is pull-request code: by the time it returns every process it
    started is dead, but it may have turned ``script`` (or a directory on
    its way) into a symlink. The head script is then not written back
    through it.

    Raises:
        ScriptTamperedError: ``script`` is no longer a plain file of the tree.
    """
    if content is None:
        yield
        return
    target = head_dir / script
    original = target.read_bytes()
    mode = target.stat().st_mode
    target.write_bytes(content)
    try:
        yield
    finally:
        if not plain_file(head_dir, script):
            raise ScriptTamperedError(
                f"check script {script} changed into a symlink during the run"
            )
        target.write_bytes(original)
        target.chmod(mode)


@dataclass
class _Budget:
    """The PR's check budget, measured on ``clock``."""

    total_s: float
    clock: Callable[[], float]
    started: float = field(init=False)

    def __post_init__(self) -> None:
        """Start the budget now."""
        self.started = self.clock()

    def remaining(self) -> float:
        """Seconds left before the budget is spent."""
        return self.total_s - (self.clock() - self.started)


def _run_once(
    entry: GeneratedEntry,
    path: str,
    *,
    head_dir: Path,
    head_sha: str,
    base_sha: str,
    runner: Runner,
    timeout_s: int,
    budget: _Budget,
    script_content: bytes | None,
    env: Mapping[str, str],
) -> tuple[GeneratedState, int | None, int, str]:
    """Run one check and return ``(state, exit code, ms, output tail)``."""
    limit = min(entry.check_timeout_s, timeout_s)
    remaining = budget.remaining()
    if remaining <= 0:
        return (
            GeneratedState.SKIPPED_COST,
            None,
            0,
            f"check not run within {budget.total_s:g} s: {path}",
        )
    allowed = min(limit, remaining)
    if not plain_file(head_dir, entry.check[0]):
        return (
            GeneratedState.STALE,
            None,
            0,
            f"check script {entry.check[0]} is a symlink or leaves the head tree",
        )
    argv = _interpreted(declared_argv(entry, head_sha, base_sha), head_dir)
    started = budget.clock()
    try:
        with _script_at(head_dir, entry.check[0], script_content):
            result = runner(argv, cwd=head_dir, env=env, timeout_s=allowed, shell=False)
    except subprocess.TimeoutExpired:
        ms = round((budget.clock() - started) * 1000)
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    except ScriptTamperedError as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_script_tampered", path=path)
        return GeneratedState.STALE, None, ms, str(exc)
    except Exception as exc:
        ms = round((budget.clock() - started) * 1000)
        _log.warning("generated.check_raised", path=path, exc=type(exc).__name__)
        return GeneratedState.STALE, None, ms, type(exc).__name__
    elapsed = budget.clock() - started
    ms = round(elapsed * 1000)
    if elapsed > allowed:
        return GeneratedState.SKIPPED_COST, None, ms, f"check not run within {allowed:g} s: {path}"
    state = GeneratedState.VERIFIED if result.exit_code == 0 else GeneratedState.STALE
    return state, result.exit_code, ms, tail_lines(result.output)


def run_checks(
    touched: Mapping[str, GeneratedEntry],
    *,
    head_dir: Path,
    head_sha: str,
    runner: Runner,
    budget_s: int = GENERATED_CHECKS_TOTAL_BUDGET_S,
    timeout_s: int = GENERATED_CHECK_TIMEOUT_S,
    changed_files: Sequence[str] = (),
    base_sha: str = "",
    base_blob: Callable[[str], bytes | None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    env: Mapping[str, str] | None = None,
    fresh_tree: Callable[[], AbstractContextManager[Path]] | None = None,
) -> list[GeneratedCheck]:
    """Run each touched artefact's check against the head tree, in declaration order.

    Never called by the review job (G2): only ``repoach review generated``
    runs it, in the secret-free verification job. Every run gets
    ``shell=False`` and :func:`scrubbed_env`. When ``check[0]`` itself
    changed in the diff, the check runs twice against the head tree: with
    the base-ref script, then with the head script (D3).

    A run is pull-request code and may rewrite the tree it runs in, so
    with ``fresh_tree`` each run gets a tree of its own; and a run that
    leaves the artefact's bytes different from the blob it was bound to
    ends stale (``the check changed <path>``), never verified.

    Args:
        touched: The declared artefacts the diff changes.
        head_dir: The head tree.
        head_sha: The head SHA (``{head}``).
        runner: The subprocess seam.
        budget_s: Seconds every run of the PR may take together.
        timeout_s: Seconds one run may take, at most.
        changed_files: The paths the diff changes.
        base_sha: The base SHA (``{base}``).
        base_blob: Reads a path at the base (the D3 base script).
        clock: The monotonic clock the budget and durations read.
        env: The environment; :func:`scrubbed_env` when omitted.
        fresh_tree: Makes a new tree at the head for each run; every run
            uses ``head_dir`` itself when omitted.

    Returns:
        One :class:`GeneratedCheck` per run; an artefact deleted at the
        head gets none.
    """
    environment = dict(env) if env is not None else scrubbed_env()
    budget = _Budget(float(budget_s), clock)
    checks: list[GeneratedCheck] = []
    for path, entry in touched.items():
        artefact = head_dir / path
        if symlinked(head_dir, path):
            checks.append(symlink_row(entry, path, head_sha=head_sha, changed_files=changed_files))
            continue
        if not artefact.is_file():
            continue
        artefact_sha = sha256_hex(artefact.read_bytes())
        sources, producers = changed_inputs(entry, path, changed_files)
        script = entry.check[0]
        template = GeneratedCheck(
            path=path,
            argv=declared_argv(entry, head_sha, base_sha),
            head_sha=head_sha,
            artefact_sha256=artefact_sha,
            sources_changed=sources,
            state=GeneratedState.NOT_RUN,
            exit_code=None,
            duration_ms=0,
            output_tail="",
            producer_inputs_changed=producers,
        )
        if not (head_dir / script).is_file():
            checks.append(
                replace(
                    template,
                    state=GeneratedState.CHECK_ABSENT,
                    output_tail=f"check script {script} absent at {head_sha[:7]}",
                )
            )
            continue
        if not plain_file(head_dir, script):
            checks.append(
                replace(
                    template,
                    state=GeneratedState.STALE,
                    output_tail=f"check script {script} is a symlink or leaves the head tree",
                )
            )
            continue
        runs: list[tuple[ScriptRef, bytes | None]] = [("head", None)]
        if script in changed_files and base_blob is not None:
            base_script = base_blob(script)
            if base_script is not None:
                runs = [("base", base_script), ("head", None)]
        for ref, content in runs:
            with fresh_tree() if fresh_tree is not None else nullcontext(head_dir) as tree:
                state, exit_code, ms, tail = _run_once(
                    entry,
                    path,
                    head_dir=tree,
                    head_sha=head_sha,
                    base_sha=base_sha,
                    runner=runner,
                    timeout_s=timeout_s,
                    budget=budget,
                    script_content=content,
                    env=environment,
                )
                if state is GeneratedState.VERIFIED and _artefact_moved(tree, path, artefact_sha):
                    state, tail = GeneratedState.STALE, f"the check changed {path}"
            if state is GeneratedState.VERIFIED and not (sources or producers):
                state = GeneratedState.NO_SOURCE_CHANGED
            checks.append(
                replace(
                    template,
                    state=state,
                    exit_code=exit_code,
                    duration_ms=ms,
                    output_tail=tail,
                    script_ref=ref,
                    runs_for_path=len(runs),
                )
            )
    return checks


def generated_findings(
    checks: Sequence[GeneratedCheck], *, pr_number: int, round_n: int
) -> list[Finding]:
    """Build one blocking ``generated_artefact`` finding per path the checks do not prove (G3).

    Args:
        checks: The runs of this head.
        pr_number: The pull request.
        round_n: The review round.

    Returns:
        The findings, ``verified`` (the check is the verification).
    """
    findings: list[Finding] = []
    for path, runs in by_path(checks).items():
        if path_verified(runs):
            continue
        causes = [check_cause(run) for run in runs if run.state is not GeneratedState.VERIFIED]
        head = runs[0].head_sha
        findings.append(
            Finding(
                pr_number=pr_number,
                head_sha=head,
                round=round_n,
                finder=GENERATED_FINDER,
                claim_type=ClaimType.GENERATED_ARTEFACT,
                severity=Severity.BLOCKING,
                file=path,
                line_start=0,
                line_end=0,
                claim=f"{_CLAIM_PREFIX}{path}",
                evidence_pointer=f"{path}: proved by {' '.join(runs[0].argv)}",
                status=FindingStatus.VERIFIED,
                verification_method=GENERATED_FINDER,
                verification_result="; ".join(causes),
                checked_at_sha=head,
            )
        )
    return findings


def record_generated_findings(
    db_path: Path, checks: Sequence[GeneratedCheck], *, pr_number: int, round_n: int
) -> int:
    """Persist :func:`generated_findings` once per head; return how many were new.

    Args:
        db_path: The findings ledger.
        checks: The runs of this head.
        pr_number: The pull request.
        round_n: The review round.

    Returns:
        How many findings were new at the head.
    """
    findings = generated_findings(checks, pr_number=pr_number, round_n=round_n)
    if not findings:
        return 0
    init_findings_schema(db_path)
    head = findings[0].head_sha
    at_head = {
        finding_fingerprint(row)
        for row in fetch_findings(db_path, pr_number)
        if row.head_sha == head
    }
    recorded = 0
    for finding in findings:
        if finding_fingerprint(finding) in at_head:
            continue
        record_finding(db_path, finding)
        recorded += 1
    return recorded


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


__all__ = [
    "ReviewScope",
    "generated_findings",
    "record_generated_findings",
    "report_json",
    "report_lines",
    "review_scope",
    "run_checks",
    "summarise",
    "summary_paragraph",
]
