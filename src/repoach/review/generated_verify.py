"""``repoach review generated``: run the touched checks at a head, secret-free.

The verification half of SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ (G2,
G7): read the declarations at the base, materialise a tree at the head,
run each touched check through
:data:`~repoach.review.generated_artefacts.subprocess_runner` (looked up
at call time) and build the ``format:review-generated-json`` evidence.
"""

from __future__ import annotations

import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import generated_artefacts as artefacts
from .generated_artefacts import (
    GeneratedCheck,
    GeneratedTiming,
    Runner,
    blob_at,
    declared_at_base,
    evidence_payload,
    merge_base,
    run_checks,
    touched_artefacts,
)


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``root`` and capture its bytes."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


@contextmanager
def head_tree(root: Path, sha: str, state_dir: Path) -> Iterator[Path]:
    """Yield a tree at ``sha``: the checkout when it is there and clean, else a detached worktree.

    The worktree is removed on exit.
    """
    at = _git(root, "rev-parse", "HEAD").stdout.decode().strip()
    dirty = _git(root, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if at == sha and not dirty:
        yield root
        return
    target = state_dir / "generated" / sha[:12]
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        _git(root, "worktree", "remove", "--force", str(target))
    added = _git(root, "worktree", "add", "--detach", "--force", str(target), sha)
    if added.returncode != 0:
        raise RuntimeError(added.stderr.decode(errors="replace").strip()[:300])
    try:
        yield target
    finally:
        _git(root, "worktree", "remove", "--force", str(target))


class GeneratedRefusalError(RuntimeError):
    """``repoach review generated`` cannot run: a ref does not resolve or the base is invalid."""


def _resolve(root: Path, ref: str) -> str | None:
    """Return the commit ``ref`` names in ``root``, ``None`` when it does not resolve."""
    found = _git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def verify_generated(
    root: Path,
    *,
    pr_number: int,
    head: str,
    base_ref: str,
    state_dir: Path,
    runner: Runner | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[dict[str, Any], list[GeneratedCheck], GeneratedTiming]:
    """Run the touched checks of a PR at its head and build the evidence (``repoach review generated``).

    The verification job's whole work, secret-free (G2, G7): the
    declarations are read at ``base_ref``, the changed paths between the
    merge base and the head, the checks run against a tree at the head
    (the checkout when it is there and clean, else a detached worktree
    under ``state_dir``), and the wall time is split into ``checks_ms``
    and ``overhead_ms``.

    Args:
        root: The checkout.
        pr_number: The pull request.
        head: The head SHA (or a ref naming it).
        base_ref: The base ref (``origin/develop``).
        state_dir: Where a worktree of the head may be created.
        runner: The subprocess seam; :func:`~repoach.review.generated_artefacts.subprocess_runner`, looked up
            at call time, when omitted.
        clock: The monotonic clock.

    Returns:
        ``(evidence payload, runs, timing)``.

    Raises:
        GeneratedRefusalError: The head or the base does not resolve.
        ConfigError: The base declarations are invalid.
    """
    started = clock()
    head_sha = _resolve(root, head)
    if head_sha is None:
        raise GeneratedRefusalError(f"head {head!r} does not resolve to a commit")
    if _resolve(root, base_ref) is None:
        raise GeneratedRefusalError(f"base {base_ref!r} does not resolve to a commit")
    base_sha = merge_base(root, base_ref, head_sha) or base_ref
    changed = changed_paths(root, base_sha, head_sha)
    touched = touched_artefacts(declared_at_base(root, base_ref), changed)
    checks: list[GeneratedCheck] = []
    if touched:
        with head_tree(root, head_sha, state_dir) as tree:
            checks = run_checks(
                touched,
                head_dir=tree,
                head_sha=head_sha,
                runner=runner if runner is not None else artefacts.subprocess_runner,
                changed_files=changed,
                base_sha=base_sha,
                base_blob=lambda path: blob_at(root, base_sha, path),
                clock=clock,
            )
    checks_ms = sum(check.duration_ms for check in checks)
    overhead_ms = max(round((clock() - started) * 1000) - checks_ms, 0)
    timing = GeneratedTiming(checks_ms=checks_ms, overhead_ms=overhead_ms)
    payload = evidence_payload(
        pr_number=pr_number, head_sha=head_sha, base_sha=base_sha, checks=checks, timing=timing
    )
    return payload, checks, timing


def changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return the paths changed between ``base_sha`` and ``head_sha``."""
    listed = _git(root, "diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in listed.stdout.decode().splitlines() if line]


__all__ = [
    "GeneratedRefusalError",
    "changed_paths",
    "head_tree",
    "verify_generated",
]
