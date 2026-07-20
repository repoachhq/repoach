"""Supersede a decomposed parent spec by removing it (SP-DEVAGENT-WIRE).

Slice 5 of the real-coding-agent arc (see ``docs/devagent_architecture.md``). When a
governed parent spec is decomposed into sub-specs whose ``owns`` partition the parent's,
the parent file still declares those paths — so the arch disjointness gate
(:meth:`repoach.arch.registry.Registry.disjointness_violations`) would flag the
parent and its sub-specs co-owning the partitioned artifacts.

The operator-calibrated lifecycle (2026-06-28) closes that overlap by **deleting** the
parent spec file: ``git rm`` removes the parent node from the corpus entirely, so the
sub-specs become the sole owners and the gate stays green with no ``status``-awareness
needed in the registry. The deletion is staged into the **same commit** as the sub-spec
files by the caller, leaving the branch coherent.

The helper is self-contained (a stdlib ``subprocess`` git invocation — no new
cross-component import edge), jailed to ``docs/specs/`` under the repository root, and
never raises: a refusal or git failure is returned as a short error string for the
caller to surface.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

from ..core.logging import get_logger
from .spec import SPECS_DIR, SpecPlan

_log = get_logger(__name__)


def _run_git(repo_root: Path, *args: str, timeout_s: int = 60) -> tuple[int, str]:
    """Run one git command in *repo_root*, returning ``(rc, output_tail)``."""
    git = shutil.which("git") or "git"
    proc = subprocess.run(
        [git, *args],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    output = (proc.stdout + proc.stderr).strip()
    return proc.returncode, output[-400:]


def supersede_parent_on_decompose(
    repo_root: Path,
    parent_spec: SpecPlan,
    *,
    staged_subspecs: Sequence[Path],
) -> str:
    """Remove the decomposed parent spec file so its sub-specs are the sole owners.

    The parent's ``owns`` partition has been handed to the sub-specs; leaving the
    parent on disk would double-own every partitioned path. ``git rm`` deletes the
    parent from the index and working tree, dropping its registry node so
    ``arch check`` sees a clean partition.

    Args:
        repo_root: Repository root (the git working tree).
        parent_spec: The loaded parent spec whose file is to be removed.
        staged_subspecs: The sub-spec files already written + staged for the same
            commit; a non-empty sequence is the guard that the parent is genuinely
            being replaced before it is deleted.

    Returns:
        ``""`` on success; a short error string when the parent path escapes
        ``docs/specs/``, when no sub-specs replace it, or when ``git rm`` fails. The
        function never raises.
    """
    if not staged_subspecs:
        return "refusing to supersede parent: no sub-specs staged to replace it"

    specs_dir = (repo_root / SPECS_DIR).resolve()
    try:
        parent_path = (repo_root / parent_spec.file_path).resolve()
    except (OSError, ValueError) as exc:
        return f"could not resolve parent spec path: {str(exc)[:120]}"
    if not parent_path.is_relative_to(specs_dir):
        return f"refusing to supersede parent outside {SPECS_DIR}: {parent_spec.file_path}"
    if not parent_path.is_file():
        return f"parent spec file not found: {parent_spec.file_path}"

    rel = parent_path.relative_to(repo_root).as_posix()
    rc, out = _run_git(repo_root, "rm", "--", rel)
    if rc != 0:
        return f"git rm parent spec failed: {out}"
    _log.info(
        "spec_supersede.parent_removed",
        spec_id=parent_spec.id,
        path=rel,
        n_sub_specs=len(staged_subspecs),
    )
    return ""


__all__ = ["supersede_parent_on_decompose"]
