"""Declared generated artefacts: read at the base, summarised for the reviewers, checked.

A file a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. The
repository declares such a file under ``[[review.generated]]`` with the
command that proves the derivation (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

This module scopes a review to the declarations read at the base ref,
replaces a touched artefact's diff unit by one summary paragraph before any
reviewer lens sees it (:func:`summarise`), and runs the checks for the
secret-free verification job (:func:`run_checks`). The rest of the flow
lives beside it:

- :mod:`~repoach.review.generated_config`: the declarations, read at
  the base ref;
- :mod:`~repoach.review.generated_types`: the rows, the entry, the causes
  and the blocking ``generated_artefact`` findings every module shares;
- :mod:`~repoach.review.generated_report`: the JSON entries and the
  report lines;
- :mod:`~repoach.review.generated_process`: how one check process runs;
- :mod:`~repoach.review.generated_git`: the git reads (base blobs, merge
  base, changed paths);
- :mod:`~repoach.review.generated_verify`: ``repoach review generated``;
- :mod:`~repoach.review.generated_evidence`: the evidence file and how the
  review reads it back;
- :mod:`~repoach.review.generated_store` and
  :mod:`~repoach.review.generated_settle`: the ``generated_checks`` rows,
  the recorded findings, and how they settle findings of older heads,
  never through the refuter.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..core.logging import get_logger
from ..project.config import GENERATED_CHECK_TIMEOUT_S
from .diff_scoper import split_diff
from .generated_config import (
    declared_at_base,
    present_at_base,
    touched_artefacts,
)
from .generated_git import blob_at, merge_base
from .generated_process import Budget, Runner, run_once
from .generated_types import (
    GENERATED_CHECKS_TOTAL_BUDGET_S,
    GeneratedCheck,
    GeneratedEntry,
    GeneratedState,
    ScriptRef,
    artefact_triage,
    changed_inputs,
    declared_argv,
    plain_file,
    sha256_hex,
    symlinked,
)
from .secret_env import scrubbed_env

_log = get_logger(__name__)


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
    touched = present_at_base(head_dir, before, touched)
    if not touched:
        return ReviewScope(review_diff=diff, changed=changed, base_sha=before)

    def head_blob(path: str) -> bytes | None:
        """Read ``path`` at the head, ``None`` unless it is a plain file of the tree."""
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


def _artefact_moved(tree: Path, path: str, bound_sha: str) -> bool:
    """Whether the run left ``path`` other than the blob the run was bound to."""
    if symlinked(tree, path) or not (tree / path).is_file():
        return True
    return sha256_hex((tree / path).read_bytes()) != bound_sha


def _precheck(
    entry: GeneratedEntry,
    path: str,
    *,
    head_dir: Path,
    head_sha: str,
    base_sha: str,
    changed: Sequence[str],
) -> tuple[GeneratedCheck | None, GeneratedCheck | None]:
    """Triage one touched artefact before any run: ``(template, refused row)``.

    ``(None, None)`` for an artefact the head deletes (nothing to run);
    a refused row for a symlinked artefact, an absent check script or one
    reached through a symlink; otherwise the template every run of the
    artefact is built from.
    """
    content, refused = artefact_triage(
        entry, path, head_dir=head_dir, head_sha=head_sha, changed=changed
    )
    if content is None:
        return None, refused
    sources, producers = changed_inputs(entry, path, changed)
    script = entry.check[0]
    template = GeneratedCheck(
        path=path,
        argv=declared_argv(entry, head_sha, base_sha),
        head_sha=head_sha,
        artefact_sha256=sha256_hex(content),
        sources_changed=sources,
        state=GeneratedState.NOT_RUN,
        exit_code=None,
        duration_ms=0,
        output_tail="",
        producer_inputs_changed=producers,
    )
    if not (head_dir / script).is_file():
        tail = f"check script {script} absent at {head_sha[:7]}"
        return template, replace(template, state=GeneratedState.CHECK_ABSENT, output_tail=tail)
    if not plain_file(head_dir, script):
        tail = f"check script {script} is a symlink or leaves the head tree"
        return template, replace(template, state=GeneratedState.STALE, output_tail=tail)
    return template, None


def _planned_runs(
    script: str, changed: Sequence[str], base_blob: Callable[[str], bytes | None] | None
) -> list[tuple[ScriptRef, bytes | None]]:
    """Return the runs of one check: the head script, preceded by the base script when it changed (D3)."""
    if script in changed and base_blob is not None:
        base_script = base_blob(script)
        if base_script is not None:
            return [("base", base_script), ("head", None)]
    return [("head", None)]


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

    Raises:
        Exception: Whatever ``fresh_tree`` raises when it cannot make a
            tree (:func:`~repoach.review.generated_verify.head_tree` raises
            ``RuntimeError``). A run's own failure, ``runner`` included, is
            caught and recorded as its row, never raised.
    """
    environment = dict(env) if env is not None else scrubbed_env()
    budget = Budget(float(budget_s), clock)
    checks: list[GeneratedCheck] = []
    for path, entry in touched.items():
        template, refused = _precheck(
            entry,
            path,
            head_dir=head_dir,
            head_sha=head_sha,
            base_sha=base_sha,
            changed=changed_files,
        )
        if refused is not None:
            checks.append(refused)
            continue
        if template is None:
            continue
        runs = _planned_runs(entry.check[0], changed_files, base_blob)
        for ref, content in runs:
            with fresh_tree() if fresh_tree is not None else nullcontext(head_dir) as tree:
                state, exit_code, ms, tail = run_once(
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
                if state is GeneratedState.VERIFIED and _artefact_moved(
                    tree, path, template.artefact_sha256
                ):
                    state, tail = GeneratedState.STALE, f"the check changed {path}"
            if state is GeneratedState.VERIFIED and not (
                template.sources_changed or template.producer_inputs_changed
            ):
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


__all__ = [
    "ReviewScope",
    "review_scope",
    "run_checks",
    "summarise",
    "summary_paragraph",
]
