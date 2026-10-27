"""The git reads of the generated-artefact flow (SP-GENERATED-FILES-ARE-VERIFIED-NOT-READ).

Shared by the review job (:mod:`repoach.review.generated_artefacts`:
base declarations and blobs) and the verification job
(:mod:`repoach.review.generated_verify`: refs, changed paths, the clone).
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def run_git(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    """Run one git command in ``root`` and capture its bytes (never raises on its exit)."""
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)


def blob_at(root: Path, ref: str, path: str) -> bytes | None:
    """Return ``path``'s content at ``ref``, ``None`` when it is absent there."""
    shown = run_git(root, "show", f"{ref}:{path}")
    return shown.stdout if shown.returncode == 0 else None


def merge_base(root: Path, base_ref: str, head: str) -> str | None:
    """Return the merge base of ``base_ref`` and ``head``, ``None`` when git cannot say."""
    found = run_git(root, "merge-base", base_ref, head)
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def resolve_commit(root: Path, ref: str) -> str | None:
    """Return the commit ``ref`` names in ``root``, ``None`` when it does not resolve."""
    found = run_git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if found.returncode != 0:
        return None
    return found.stdout.decode().strip() or None


def changed_paths(root: Path, base_sha: str, head_sha: str) -> list[str]:
    """Return the paths changed between ``base_sha`` and ``head_sha``."""
    listed = run_git(root, "diff", "--name-only", "--no-renames", base_sha, head_sha)
    return [line for line in listed.stdout.decode().splitlines() if line]


__all__ = ["blob_at", "changed_paths", "merge_base", "resolve_commit", "run_git"]
