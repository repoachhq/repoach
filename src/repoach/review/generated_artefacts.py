"""Declared generated artefacts: summarised for the reviewers, proved by their check.

A file a committed script derives from other committed files cannot be
reviewed by reading it: its content is a function of its sources. The
repository declares such a file under ``[[review.generated]]`` with the
command that proves the derivation (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

The flow has two halves that never share a process:

- the secret-free verification job runs ``repoach review generated``,
  which runs each touched check once at the head (twice when the check
  script itself changed, D3) through an injected :class:`Runner` with
  ``shell=False`` and :func:`~repoach.review.secret_env.scrubbed_env`,
  and writes the ``format:review-generated-json`` evidence file;
- the review job reads the declarations at the base ref, replaces the
  artefact's diff unit by one summary paragraph before any reviewer lens
  sees it (:func:`summarise`), loads the evidence bound to the reviewed
  head, records one ``generated_checks`` row per run, and records a
  blocking ``generated_artefact`` finding for every path whose derivation
  is not shown (:func:`generated_findings`).

At a later head, :func:`settle_at_head` settles those findings, and the
older oversized findings of a newly declared path, from the
``generated_checks`` rows of that head, never through the refuter.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ..core.logging import get_logger
from ..project.config import (
    CONFIG_FILE_NAME,
    GENERATED_CHECK_TIMEOUT_S,
    ConfigError,
    ProjectConfig,
    glob_matches,
    parse_project_config,
    refuse_generated_conflicts,
)
from .diff_scoper import split_diff

_log = get_logger(__name__)


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


def _entries(config: ProjectConfig) -> tuple[GeneratedEntry, ...]:
    """Turn the validated ``[[review.generated]]`` sections into entries."""
    return tuple(
        GeneratedEntry(
            path=section.path,
            check=tuple(section.check),
            sources=tuple(section.sources),
            producer_inputs=tuple(section.producer_inputs),
            check_timeout_s=section.check_timeout_s,
        )
        for section in config.review.generated
    )


def load_generated_entries(
    base_toml_text: str, path: Path, languages: Sequence[str]
) -> tuple[GeneratedEntry, ...]:
    """Parse the ``[[review.generated]]`` entries of a ``repoach.toml`` read at the base ref.

    Args:
        base_toml_text: The file's content at the base ref.
        path: Where it came from, for messages.
        languages: Languages whose files a ``path`` glob may never match,
            besides the file's own ``[project].languages``.

    Returns:
        The entries, in declaration order.

    Raises:
        ConfigError: A malformed entry, a glob matching code, or two
            entries declaring the same path; the message names the key
            and the line.
    """
    config = parse_project_config(base_toml_text, path)
    refuse_generated_conflicts(path, base_toml_text, config.review.generated, tuple(languages))
    return _entries(config)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one read-only git command in ``root`` and capture its bytes."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def blob_at(root: Path, ref: str, path: str) -> bytes | None:
    """Return ``path``'s content at ``ref``, ``None`` when it is absent there."""
    shown = _git(root, "show", f"{ref}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def merge_base(root: Path, base_ref: str, head: str) -> str | None:
    """Return the merge base of ``base_ref`` and ``head``, ``None`` when git cannot say."""
    found = _git(root, "merge-base", base_ref, head)
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def declared_at_base(root: Path, base_ref: str | None) -> tuple[GeneratedEntry, ...]:
    """Read the generated-artefact declarations of ``repoach.toml`` at the base ref (G1).

    The head never declares: an entry a PR adds applies from the next PR on.

    Args:
        root: Any checkout of the repository (refs are shared).
        base_ref: The base ref; ``None`` declares nothing.

    Returns:
        The entries, empty when the base has no file or no entry.

    Raises:
        ConfigError: The base file is invalid.
    """
    if not base_ref:
        return ()
    text = blob_at(root, base_ref, CONFIG_FILE_NAME)
    if text is None:
        return ()
    decoded = text.decode("utf-8", errors="replace")
    config = parse_project_config(decoded, Path(f"{base_ref}:{CONFIG_FILE_NAME}"))
    return _entries(config)


def touched_artefacts(
    entries: Sequence[GeneratedEntry], diff_files: Sequence[str]
) -> dict[str, GeneratedEntry]:
    """Return every changed path a declared ``path`` matches, in declaration order.

    Args:
        entries: The declarations read at the base.
        diff_files: The paths the diff changes.

    Returns:
        ``{changed path: its entry}``; empty when the diff touches none.

    Raises:
        ConfigError: Two entries match the same changed path.
    """
    touched: dict[str, GeneratedEntry] = {}
    for entry in entries:
        for path in diff_files:
            if not entry.matches(path):
                continue
            other = touched.get(path)
            if other is not None and other is not entry:
                raise ConfigError(
                    Path(CONFIG_FILE_NAME),
                    f"{path} is matched by two entries ({other.path!r} and {entry.path!r})",
                    key="review.generated",
                )
            touched[path] = entry
    return touched


def changed_inputs(
    entry: GeneratedEntry, path: str, diff_files: Sequence[str]
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Return ``(sources changed, producer inputs changed)`` for one artefact.

    The artefact itself never counts as its own source.
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


def _sha256(content: bytes | None) -> str:
    """Return the hex sha256 of ``content``, empty for an absent file."""
    return hashlib.sha256(content).hexdigest() if content is not None else ""


def summary_paragraph(
    path: str,
    entry: GeneratedEntry,
    *,
    before: bytes | None,
    after: bytes | None,
    diff_files: Sequence[str],
) -> str:
    """Build the one paragraph every reviewer lens sees instead of the artefact's diff (G2).

    Sizes, hashes and paths only (NG1, NG2).
    """
    size_before = f"{len(before)}" if before is not None else "absent"
    size_after = f"{len(after)}" if after is not None else "deleted"
    sha_before = _sha256(before)[:7] or "none"
    sha_after = _sha256(after)[:7] or "none"
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
) -> str:
    """Return ``diff`` with each touched artefact's unit replaced by its summary paragraph.

    The unit keeps its ``diff --git`` header so the path stays a changed
    path; nothing else of the artefact reaches a reviewer.

    Args:
        diff: The PR's unified diff.
        touched: The declared artefacts the diff changes.
        base_blob: Reads a path at the base.
        head_blob: Reads a path at the head.

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
            after=head_blob(unit.path),
            diff_files=paths,
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
    """

    review_diff: str
    touched: Mapping[str, GeneratedEntry] = field(default_factory=dict)
    changed: tuple[str, ...] = ()


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
        return target.read_bytes() if target.is_file() else None

    summarised = summarise(
        diff,
        touched,
        base_blob=lambda path: blob_at(head_dir, before, path),
        head_blob=head_blob,
    )
    return ReviewScope(review_diff=summarised, touched=touched, changed=changed)


__all__ = [
    "GeneratedEntry",
    "ReviewScope",
    "blob_at",
    "declared_at_base",
    "load_generated_entries",
    "merge_base",
    "review_scope",
    "summarise",
    "touched_artefacts",
]
