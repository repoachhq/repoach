"""``repoach review generated``: run the touched checks at a head, secret-free.

The verification half of SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ (G2,
G7): read the declarations at the base, clone a tree at the head,
run each touched check through the injected runner (by default
:func:`~repoach.review.generated_artefacts.subprocess_runner`) and build
the ``format:review-generated-json`` evidence.
"""

from __future__ import annotations

import shutil
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .generated_artefacts import (
    GeneratedCheck,
    GeneratedTiming,
    Runner,
    blob_at,
    declared_at_base,
    evidence_payload,
    merge_base,
    run_checks,
    run_git,
    subprocess_runner,
    touched_artefacts,
)


@contextmanager
def head_tree(root: Path, sha: str, state_dir: Path) -> Iterator[Path]:
    """Yield a throwaway clone of ``root`` at ``sha``, removed on exit.

    The check is pull-request code: it never runs in the checkout, nor in
    a worktree sharing the checkout's ``.git``, where it could plant a
    hook or a config the next job of the same runner would execute. The
    clone has its own git directory and borrows the checkout's objects
    read-only (``--shared``).

    Raises:
        RuntimeError: The clone or its checkout of ``sha`` failed.
    """
    target = state_dir / "generated" / sha[:12]
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        for args in (
            ("clone", "--quiet", "--shared", "--no-checkout", str(root), str(target)),
            ("-C", str(target), "checkout", "--quiet", "--detach", sha),
        ):
            made = run_git(root, *args)
            if made.returncode != 0:
                raise RuntimeError(made.stderr.decode(errors="replace").strip()[:300])
        yield target
    finally:
        shutil.rmtree(target, ignore_errors=True)


class GeneratedRefusalError(RuntimeError):
    """``repoach review generated`` cannot run: a ref does not resolve or the base is invalid."""


def _resolve(root: Path, ref: str) -> str | None:
    """Return the commit ``ref`` names in ``root``, ``None`` when it does not resolve."""
    found = run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
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
    runner: Runner = subprocess_runner,
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
        runner: The subprocess seam.
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
                runner=runner,
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
    listed = run_git(root, "diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in listed.stdout.decode().splitlines() if line]


__all__ = [
    "GeneratedRefusalError",
    "changed_paths",
    "head_tree",
    "verify_generated",
]
